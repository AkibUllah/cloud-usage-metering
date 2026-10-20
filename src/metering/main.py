"""Application factory."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from sqlalchemy.engine import Engine

from . import __version__
from .config import Settings, get_settings
from .database import Base, make_engine, make_session_factory
from .routers import budgets, health, invoices, plans, tenants, usage

log = logging.getLogger("metering")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    engine = engine or make_engine(settings.database_url)
    Base.metadata.create_all(engine)

    app = FastAPI(
        title="Cloud Usage Metering",
        version=__version__,
        description=(
            "Ingest usage samples, rate them against pricing plans, "
            "generate invoices and budget alerts."
        ),
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "%s %s -> %s %.1fms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response

    for router in (
        health.router,
        tenants.router,
        plans.router,
        usage.router,
        invoices.router,
        budgets.router,
    ):
        app.include_router(router)
    return app
