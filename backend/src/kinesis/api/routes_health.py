"""Health, ops metrics and product statistics."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from kinesis import __version__
from kinesis.api.deps import get_service
from kinesis.jobs.service import JobService
from kinesis.schemas import ComponentHealth, HealthReport, ProductStats

CHECK_TIMEOUT_S = 15.0

router = APIRouter(prefix="/v1", tags=["ops"])


@router.get("/health", response_model=HealthReport, summary="Liveness")
async def health() -> HealthReport:
    return HealthReport(status="ok", version=__version__)


@router.get(
    "/health/providers",
    response_model=HealthReport,
    summary="Model provider, Redis and Blender reachability",
    description=(
        "Checks each dependency once, in parallel. `status` is `ok` only when every component "
        "is healthy; a failing optional component (Redis, the model provider) makes it "
        "`degraded`, because Kinesis keeps working without them."
    ),
)
async def provider_health(service: Annotated[JobService, Depends(get_service)]) -> HealthReport:
    async def timed(name: str, check: Callable[[], Awaitable[tuple[bool, str]]]) -> ComponentHealth:
        started = time.monotonic()
        try:
            ok, detail = await asyncio.wait_for(check(), CHECK_TIMEOUT_S)
        except Exception as exc:  # a health check must never raise
            ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]
        return ComponentHealth(
            name=name, ok=ok, detail=detail, latency_ms=int((time.monotonic() - started) * 1000)
        )

    async def provider() -> tuple[bool, str]:
        health = await service.provider.health_check()
        return health.ok, f"{service.provider.name}: {health.detail}"

    async def cache() -> tuple[bool, str]:
        if service.cache is None:
            return True, "no cache configured"
        key = "kinesis:health:probe"
        await service.cache.set(key, "1", ttl_s=10)
        degraded = getattr(service.cache, "degraded_calls", 0)
        ok = await service.cache.get(key) == "1"
        if getattr(service.cache, "degraded_calls", 0) > degraded:
            return False, "redis unreachable; using the in-process fallback"
        return ok, type(service.cache).__name__

    components = await asyncio.gather(
        timed("model_provider", provider),
        timed("redis", cache),
        timed("blender", service.runner.health_check),
    )
    status = "ok" if all(c.ok for c in components) else "degraded"
    return HealthReport(status=status, version=__version__, components=tuple(components))


@router.get(
    "/metrics",
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}, "description": "Prometheus exposition"}},
    summary="Prometheus metrics",
)
async def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get(
    "/stats/product",
    response_model=ProductStats,
    summary="Product metrics (acceptance, agreement, slip reduction, cost)",
)
async def product_stats(service: Annotated[JobService, Depends(get_service)]) -> ProductStats:
    return await service.product_stats()
