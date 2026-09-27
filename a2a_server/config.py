"""Host, port, and database path for a local process or an AgentCore container."""

import os
from pathlib import Path


def target() -> str:
    raw = os.environ.get("A2A_TARGET", "local").strip().lower()
    if raw not in {"local", "agentcore"}:
        raise ValueError("A2A_TARGET must be 'local' or 'agentcore'")
    return raw


def listen_host() -> str:
    if "A2A_HOST" in os.environ:
        return os.environ["A2A_HOST"]
    if target() == "agentcore":
        return "0.0.0.0"
    return "127.0.0.1"


def listen_port() -> int:
    if "A2A_PORT" in os.environ:
        return int(os.environ["A2A_PORT"])
    if target() == "agentcore":
        return 9000
    return 8123


def base_url() -> str:
    """URL advertised on the agent card."""

    override = os.environ.get("AGENTCORE_RUNTIME_URL", "").strip()
    if override:
        return override.rstrip("/")
    host = listen_host()
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return f"http://{host}:{listen_port()}"


def database_path() -> Path:
    if "A2A_DB_PATH" in os.environ:
        configured = os.environ["A2A_DB_PATH"]
    elif target() == "agentcore":
        configured = "/mnt/efs/a2a_tasks.db"
    else:
        configured = "data/a2a_tasks.db"
    return Path(configured).expanduser().resolve()
