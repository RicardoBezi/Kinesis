"""Prometheus metrics, served at ``/v1/metrics``. Names and labels follow docs/METRICS.md §2.

Labels are low-cardinality enums only (node ids, model ids, outcomes), never job ids.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

from kinesis.errors import (
    CircuitOpen,
    ProviderClientError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
)

_SECONDS = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)

JOB_DURATION = Histogram(
    "kinesis_job_duration_seconds",
    "Job wall time from creation to a review or terminal state",
    ["outcome"],
    buckets=_SECONDS,
)
NODE_DURATION = Histogram(
    "kinesis_node_duration_seconds",
    "DAG node wall time, including retries",
    ["node"],
    buckets=_SECONDS,
)
WORKER_DURATION = Histogram(
    "kinesis_worker_duration_seconds",
    "Blender worker command wall time",
    ["command", "runner"],
    buckets=_SECONDS,
)
RENDER_DURATION = Histogram(
    "kinesis_render_duration_seconds",
    "Preview render time reported by the worker",
    ["runner"],
    buckets=_SECONDS,
)
PROVIDER_CALLS = Counter(
    "kinesis_provider_calls_total", "Model provider calls", ["model", "task", "outcome"]
)
PROVIDER_LATENCY = Histogram(
    "kinesis_provider_latency_seconds",
    "Model provider call latency",
    ["model", "task"],
    buckets=_SECONDS,
)
PROVIDER_TOKENS = Counter(
    "kinesis_provider_tokens_total", "Tokens used by model calls", ["model", "kind"]
)
PROVIDER_COST = Counter(
    "kinesis_provider_cost_usd_total", "Estimated model cost from catalog prices", ["model"]
)
CACHE_REQUESTS = Counter("kinesis_cache_requests_total", "Cache lookups", ["namespace", "result"])
RETRIES = Counter("kinesis_retries_total", "Node and call retries", ["node"])
CANDIDATES = Counter("kinesis_candidates_total", "Finished candidates", ["status"])
PLAN_SOURCE = Counter("kinesis_plan_source_total", "Executed plans by source", ["source"])


def provider_error_outcome(exc: BaseException) -> str:
    """The ``outcome`` label for a failed provider call."""
    if isinstance(exc, CircuitOpen):
        return "circuit_open"
    if isinstance(exc, ProviderTimeout):
        return "timeout"
    if isinstance(exc, ProviderRateLimited):
        return "429"
    if isinstance(exc, ProviderServerError):
        return "5xx"
    if isinstance(exc, ProviderClientError):
        return "4xx"
    return "error"


def cache_namespace(key: str) -> str:
    parts = key.split(":")
    return parts[1] if len(parts) > 2 and parts[0] == "kinesis" else "other"


__all__ = [
    "CACHE_REQUESTS",
    "CANDIDATES",
    "JOB_DURATION",
    "NODE_DURATION",
    "PLAN_SOURCE",
    "PROVIDER_CALLS",
    "PROVIDER_COST",
    "PROVIDER_LATENCY",
    "PROVIDER_TOKENS",
    "RENDER_DURATION",
    "RETRIES",
    "WORKER_DURATION",
    "cache_namespace",
    "provider_error_outcome",
]
