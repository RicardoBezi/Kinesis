"""Prometheus metrics (served at ``/v1/metrics``). Labels are low-cardinality enums only."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

JOBS_FINISHED = Counter(
    "kinesis_jobs_finished_total", "Jobs that reached a terminal or review state", ["status"]
)
NODE_SECONDS = Histogram(
    "kinesis_node_seconds",
    "DAG node wall time, including retries",
    ["node", "status"],
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)
PLAN_FALLBACKS = Counter(
    "kinesis_plan_fallbacks_total", "Plans that fell back to the deterministic presets", ["code"]
)

__all__ = ["JOBS_FINISHED", "NODE_SECONDS", "PLAN_FALLBACKS"]
