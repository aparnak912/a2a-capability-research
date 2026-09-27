"""Thin A2A wrapper around the shared agent. The agent does not import this."""

import asyncio
import logging
import uuid

from a2a.helpers import new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types.a2a_pb2 import TaskState
from a2a_server.activity import begin, end
from agent.work import iter_work


logger = logging.getLogger(__name__)

AGENT_ID = "demo-tracker"


def _waiting_for_input(context: RequestContext) -> bool:
    task = context.current_task
    if task is None:
        return False
    return task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED


class DemoExecutor(AgentExecutor):
    """Publish A2A task events for one shared agent run."""

    def __init__(self) -> None:
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            raise RuntimeError("A2A request context is missing task or context id")

        cancel_event = asyncio.Event()
        self._cancel_events[task_id] = cancel_event
        message = context.get_user_input()
        updater = TaskUpdater(event_queue, task_id, context_id)
        begin()
        try:
            if _waiting_for_input(context):
                logger.info(
                    "agent_id=%s a2a_task_id=%s event=resumed answer=%r",
                    AGENT_ID,
                    task_id,
                    message,
                )
                await updater.add_artifact(
                    [new_text_part(message)],
                    name="result",
                )
                await updater.complete()
                return

            logger.info(
                "agent_id=%s a2a_task_id=%s event=working input=%r",
                AGENT_ID,
                task_id,
                message,
            )
            # A Task must be published before status or artifact events.
            # This first event is what returnImmediately returns to the caller.
            history = [context.message] if context.message is not None else []
            await event_queue.enqueue_event(
                new_task(
                    task_id,
                    context_id,
                    TaskState.TASK_STATE_WORKING,
                    history=history,
                )
            )

            progress_id = str(uuid.uuid4())
            first_progress = True
            async for step in iter_work(message, cancel_event):
                if cancel_event.is_set():
                    logger.info(
                        "agent_id=%s a2a_task_id=%s event=stop-for-cancel",
                        AGENT_ID,
                        task_id,
                    )
                    return
                if step.kind == "input":
                    logger.info(
                        "agent_id=%s a2a_task_id=%s event=input-required text=%s",
                        AGENT_ID,
                        task_id,
                        step.text,
                    )
                    await updater.requires_input()
                    return
                if step.kind == "progress":
                    logger.info(
                        "agent_id=%s a2a_task_id=%s event=progress text=%s",
                        AGENT_ID,
                        task_id,
                        step.text,
                    )
                    await updater.add_artifact(
                        [new_text_part(step.text)],
                        artifact_id=progress_id,
                        name="progress",
                        append=not first_progress,
                        last_chunk=False,
                    )
                    first_progress = False
                    continue
                if cancel_event.is_set():
                    return
                logger.info(
                    "agent_id=%s a2a_task_id=%s event=completed text=%s",
                    AGENT_ID,
                    task_id,
                    step.text,
                )
                await updater.add_artifact(
                    [new_text_part(step.text)],
                    name="result",
                )
                await updater.complete()
        except asyncio.CancelledError:
            logger.info(
                "agent_id=%s a2a_task_id=%s event=producer-cancelled",
                AGENT_ID,
                task_id,
            )
            raise
        finally:
            end()
            self._cancel_events.pop(task_id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id
        if not task_id or not context_id:
            return
        logger.info(
            "agent_id=%s a2a_task_id=%s event=cancel",
            AGENT_ID,
            task_id,
        )
        cancel_event = self._cancel_events.get(task_id)
        if cancel_event is not None:
            cancel_event.set()
        await TaskUpdater(event_queue, task_id, context_id).cancel()
