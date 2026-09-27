"""Deterministic agent work used to prove tracking.

There is no model call. A slow command sleeps one second at a time and
reports each second, so a caller can observe a task that is still running.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
import asyncio


_MAX_SLOW_SECONDS = 30


@dataclass(frozen=True)
class WorkEvent:
    """One step the A2A wrapper can turn into a protocol event."""

    kind: str
    text: str


def parse_message(message: str) -> tuple[str, int, str]:
    """Return (kind, seconds, label) for a user message.

    Commands:
    - ``slow 4 alpha`` waits 4 seconds and labels the run ``alpha``
    - ``add 2 3`` returns the sum
    - anything else is returned unchanged
    """

    parts = message.strip().split()
    if not parts:
        return "echo", 0, ""
    if parts[0] == "slow":
        seconds = 4
        rest = parts[1:]
        if rest and rest[0].isdigit():
            seconds = min(int(rest[0]), _MAX_SLOW_SECONDS)
            rest = rest[1:]
        label = " ".join(rest) if rest else "slow"
        return "slow", max(seconds, 1), label
    if parts[0] == "add" and len(parts) == 3:
        return "add", 0, message
    return "echo", 0, message


async def _wait(seconds: float, cancel_event: asyncio.Event | None) -> bool:
    """Wait up to ``seconds``. Return True if cancellation was requested."""

    if cancel_event is None:
        await asyncio.sleep(seconds)
        return False
    try:
        await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


async def iter_work(
    message: str,
    cancel_event: asyncio.Event | None = None,
) -> AsyncIterator[WorkEvent]:
    """Run one request and yield progress, then one final result.

    ``cancel_event`` stops a slow run between ticks. The A2A wrapper owns
    that event. This function still does not import the protocol.
    """

    kind, seconds, label = parse_message(message)
    if kind == "slow":
        for second in range(1, seconds + 1):
            if await _wait(1, cancel_event):
                return
            yield WorkEvent("progress", f"{label} tick {second}/{seconds}")
        yield WorkEvent("result", f"{label} finished after {seconds}s")
        return
    if kind == "add":
        _command, left, right = message.strip().split()
        yield WorkEvent("result", str(int(left) + int(right)))
        return
    yield WorkEvent("result", label)
