"""Run the local agent_id gateway: ``uv run python -m gateway``."""

import logging
import os

import uvicorn

from gateway.app import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    host = os.environ.get("GATEWAY_HOST", "127.0.0.1")
    port = int(os.environ.get("GATEWAY_PORT", "8124"))
    logging.getLogger(__name__).info(
        "agent_id gateway listening at http://%s:%s",
        host,
        port,
    )
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
