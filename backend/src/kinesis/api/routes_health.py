"""Health, ops metrics and product statistics."""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from kinesis import __version__
from kinesis.api.errors import not_implemented, problem_responses
from kinesis.schemas import HealthReport, ProductStats

router = APIRouter(prefix="/v1", tags=["ops"])


@router.get("/health", response_model=HealthReport, summary="Liveness")
async def health() -> HealthReport:
    return HealthReport(status="ok", version=__version__)


@router.get(
    "/health/providers",
    response_model=HealthReport,
    responses=problem_responses(501),
    summary="Model provider, Redis and Blender reachability",
)
async def provider_health() -> HealthReport:
    raise not_implemented("Phase 4")


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
    responses=problem_responses(501),
    summary="Product metrics (acceptance, agreement, slip reduction, cost)",
)
async def product_stats() -> ProductStats:
    raise not_implemented("Phase 5")
