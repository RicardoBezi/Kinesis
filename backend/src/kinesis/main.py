"""Application factory. Run with ``uv run task run``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from kinesis import __version__
from kinesis.api import routes_artifacts, routes_health, routes_jobs, routes_scenes
from kinesis.api.deps import ensure_service
from kinesis.api.errors import install_error_handlers
from kinesis.jobs.service import JobService
from kinesis.observability.context import configure_logging
from kinesis.settings import get_settings

log = logging.getLogger("kinesis.api")


def create_app(service: JobService | None = None) -> FastAPI:
    """Build the app. Tests pass a ``service`` (fake runner, temp storage); otherwise one is
    built from settings on first use, so importing the app never touches the disk."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(get_settings().kinesis_log_level)
        svc = ensure_service(app)
        recovered = await svc.recover()
        if recovered:
            log.warning("jobs.recovered_after_restart", extra={"count": recovered})
        yield

    app = FastAPI(
        title="Kinesis API",
        version=__version__,
        summary="Localized animation repair: measure, propose, compare, apply.",
        description=(
            "AI plans and evaluates; deterministic code executes; the animator decides. "
            "Every error body is an RFC 7807 Problem carrying a machine-readable `code`."
        ),
        # The models contain no computed fields, so one schema per model serves both
        # requests and responses. This keeps the Kotlin codegen to one class per model.
        separate_input_output_schemas=False,
        lifespan=lifespan,
    )
    app.state.service = service
    install_error_handlers(app)
    for module in (routes_scenes, routes_jobs, routes_artifacts, routes_health):
        app.include_router(module.router)

    return app
