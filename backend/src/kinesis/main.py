"""Application factory. Run with ``uv run task run``."""

from __future__ import annotations

from fastapi import FastAPI

from kinesis import __version__
from kinesis.api import routes_artifacts, routes_health, routes_jobs, routes_scenes
from kinesis.api.errors import install_error_handlers


def create_app() -> FastAPI:
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
    )
    install_error_handlers(app)
    for module in (routes_scenes, routes_jobs, routes_artifacts, routes_health):
        app.include_router(module.router)
    return app
