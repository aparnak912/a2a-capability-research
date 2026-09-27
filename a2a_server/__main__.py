"""Run the local A2A server: ``uv run python -m a2a_server``."""

import logging

import uvicorn

from a2a_server.config import base_url, database_path, listen_host, listen_port, target
from a2a_server.server import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = listen_host()
    port = listen_port()
    logging.getLogger(__name__).info(
        "A2A JSON-RPC 1.0 target=%s listening at %s:%s  card=%s/.well-known/agent-card.json  db=%s",
        target(),
        host,
        port,
        base_url().rstrip("/"),
        database_path(),
    )
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
