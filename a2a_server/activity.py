"""How many agent runs are in flight. /ping reports this to AgentCore."""

import threading


_lock = threading.Lock()
_inflight = 0


def begin() -> None:
    global _inflight
    with _lock:
        _inflight += 1


def end() -> None:
    global _inflight
    with _lock:
        _inflight = max(0, _inflight - 1)


def ping_body() -> dict[str, str]:
    """Status only. A timestamp that changes on every ping keeps sessions alive forever."""

    with _lock:
        busy = _inflight > 0
    return {"status": "HealthyBusy" if busy else "Healthy"}
