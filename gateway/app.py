"""Forward JSON-RPC to one local agent, and keep push callbacks."""

from collections.abc import AsyncIterator
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


logger = logging.getLogger(__name__)

AGENT_HEADER = "X-Agent-Id"
STREAMING_METHODS = {"SendStreamingMessage", "SubscribeToTask"}


def registry() -> dict[str, str]:
    upstream = os.environ.get("A2A_UPSTREAM", "http://127.0.0.1:8123").rstrip("/")
    return {"demo-tracker": upstream}


def callback_facts(body: dict) -> tuple[str, str]:
    task = body.get("task")
    if isinstance(task, dict):
        state = task.get("status", {}).get("state", "")
        return str(task.get("id", "")), str(state)
    status_update = body.get("statusUpdate")
    if isinstance(status_update, dict):
        state = status_update.get("status", {}).get("state", "")
        return str(status_update.get("taskId", "")), str(state)
    artifact_update = body.get("artifactUpdate")
    if isinstance(artifact_update, dict):
        return str(artifact_update.get("taskId", "")), ""
    return "", ""


async def receive_callback(request: Request) -> JSONResponse:
    body = await request.json()
    request.app.state.callbacks.append(body)
    task_id, state = callback_facts(body)
    logger.info(
        "a2a_task_id=%s event=push-received state=%s",
        task_id,
        state,
    )
    return JSONResponse({"ok": True})


async def list_callbacks(request: Request) -> JSONResponse:
    return JSONResponse(request.app.state.callbacks)


async def invoke(request: Request) -> Response:
    agent_id = request.headers.get(AGENT_HEADER, "").strip()
    upstream = registry().get(agent_id)
    raw = await request.body()
    try:
        payload = json.loads(raw)
        method = str(payload.get("method", ""))
    except json.JSONDecodeError:
        logger.info("agent_id=%s event=bad-json", agent_id)
        return JSONResponse({"error": "JSON-RPC body is not valid JSON"}, status_code=400)
    if upstream is None:
        logger.info("agent_id=%s event=unknown-agent method=%s", agent_id, method)
        return JSONResponse(
            {"error": f"unknown agent_id {agent_id!r}"},
            status_code=404,
        )
    logger.info(
        "agent_id=%s event=resolved url=%s method=%s",
        agent_id,
        upstream,
        method,
    )
    headers = {
        "Content-Type": request.headers.get("content-type", "application/json"),
        "A2A-Version": request.headers.get("A2A-Version", "1.0"),
    }
    url = f"{upstream}/"
    client: httpx.AsyncClient = request.app.state.http
    if method in STREAMING_METHODS:
        sent = client.build_request("POST", url, content=raw, headers=headers)
        upstream_response = await client.send(sent, stream=True)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_response.aiter_bytes():
                    yield chunk
            finally:
                await upstream_response.aclose()

        return StreamingResponse(
            chunks(),
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type"),
        )

    upstream_response = await client.post(url, content=raw, headers=headers)
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        media_type=upstream_response.headers.get("content-type"),
    )


def create_app() -> Starlette:
    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        app.state.http = httpx.AsyncClient(timeout=None)
        app.state.callbacks = []
        try:
            yield
        finally:
            await app.state.http.aclose()

    return Starlette(
        routes=[
            Route("/callbacks", receive_callback, methods=["POST"]),
            Route("/callbacks", list_callbacks, methods=["GET"]),
            Route("/", invoke, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
