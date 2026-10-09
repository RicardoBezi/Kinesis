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

MODEL_CALLS = Counter(
    "kinesis_model_calls_total", "Model provider calls by outcome", ["task", "model", "status"]
)
MODEL_TOKENS = Counter(
    "kinesis_model_tokens_total", "Tokens used by model calls", ["model", "kind"]
)
MODEL_COST_USD = Counter(
    "kinesis_model_cost_usd_total", "Estimated model cost from catalog prices", ["model"]
)

__all__ = [
    "JOBS_FINISHED",
    "MODEL_CALLS",
    "MODEL_COST_USD",
    "MODEL_TOKENS",
    "NODE_SECONDS",
    "PLAN_FALLBACKS",
]
