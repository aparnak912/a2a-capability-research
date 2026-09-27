"""Starlette app exposing the demo agent over A2A JSON-RPC 2.0."""

from collections.abc import AsyncIterator
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import (
    BasePushNotificationSender,
    DatabasePushNotificationConfigStore,
    DatabaseTaskStore,
)
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_server.activity import ping_body
from a2a_server.card import build_agent_card
from a2a_server.config import base_url, database_path, target
from a2a_server.executor import DemoExecutor


__all__ = ["base_url", "create_app", "create_engine", "database_path"]


async def ping(_request: Request) -> JSONResponse:
    return JSONResponse(ping_body())


def create_engine(path: Path | None = None) -> AsyncEngine:
    """Open the SQLite file that holds A2A tasks."""

    db_path = path or database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def create_app(engine: AsyncEngine | None = None) -> Starlette:
    card = build_agent_card(base_url())
    db_engine = engine or create_engine()
    task_store = DatabaseTaskStore(db_engine, create_table=True)
    push_store = DatabasePushNotificationConfigStore(db_engine, create_table=True)
    push_http = httpx.AsyncClient(timeout=10)
    handler = DefaultRequestHandler(
        agent_executor=DemoExecutor(),
        task_store=task_store,
        agent_card=card,
        push_config_store=push_store,
        push_sender=BasePushNotificationSender(push_http, push_store),
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        await task_store.initialize()
        await push_store.initialize()
        try:
            yield
        finally:
            await push_http.aclose()
            await db_engine.dispose()

    routes = [
        *create_agent_card_routes(card),
        *create_jsonrpc_routes(handler, rpc_url="/"),
    ]
    if target() == "agentcore":
        routes.append(Route("/ping", ping, methods=["GET"]))

    return Starlette(routes=routes, lifespan=lifespan)
