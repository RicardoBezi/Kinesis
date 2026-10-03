"""DAG dependency resolution and join semantics (the scheduler itself is Phase 3)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from kinesis.errors import ProviderClientError, ProviderTimeout, WorkerCrashed, WorkerOutputInvalid
from kinesis.orchestration.dag import (
    DagDefinitionError,
    JoinPolicy,
    Node,
    NodeStatus,
    RetryPolicy,
    should_run,
    validate_graph,
)


async def _noop(_: Mapping[str, Any]) -> None:
    return None


def n(id_: str, *deps: str, join: JoinPolicy = JoinPolicy.ALL) -> Node:
    return Node(id=id_, fn=_noop, deps=deps, join=join)


def repair_dag() -> list[Node]:
    """The frozen repair DAG shape from ARCHITECTURE §3."""
    return [
        n("validate_input"),
        n("extract_scope", "validate_input"),
        n("contact_analysis", "extract_scope"),
        n("motion_analysis", "extract_scope"),
        n("constraint_check", "extract_scope"),
        n("defect_report", "contact_analysis", "motion_analysis", "constraint_check"),
        n("plan_repair", "defect_report"),
        n("generate_A", "plan_repair"),
        n("apply_render_A", "generate_A"),
        n("metrics_A", "apply_render_A"),
        n("generate_B", "plan_repair"),
        n("apply_render_B", "generate_B"),
        n("metrics_B", "apply_render_B"),
        n("evaluate", "metrics_A", "metrics_B", join=JoinPolicy.ANY_SUCCESS),
    ]


def test_repair_dag_topological_order_is_valid_and_stable() -> None:
    nodes = repair_dag()
    order = validate_graph(nodes)
    assert order == validate_graph(nodes)
    pos = {k: i for i, k in enumerate(order)}
    for node in nodes:
        for d in node.deps:
            assert pos[d] < pos[node.id]
    assert order[0] == "validate_input"
    assert order[-1] == "evaluate"


@pytest.mark.parametrize(
    ("nodes", "match"),
    [
        ([n("a", "missing")], "unknown dependency"),
        ([n("a", "a")], "depends on itself"),
        ([n("a", "b"), n("b", "a")], "cycle"),
        ([n("a"), n("b", "c"), n("c", "d"), n("d", "b")], "cycle"),
        ([n("a"), n("a")], "duplicate"),
        ([n("a", join=JoinPolicy.ANY_SUCCESS)], "ANY_SUCCESS requires"),
    ],
)
def test_invalid_graphs_rejected(nodes: list[Node], match: str) -> None:
    with pytest.raises(DagDefinitionError, match=match):
        validate_graph(nodes)


S, F, K, P = NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.PENDING


@pytest.mark.parametrize(
    ("join", "deps", "expected"),
    [
        (JoinPolicy.ALL, [S, S], True),
        (JoinPolicy.ALL, [S, F], False),
        (JoinPolicy.ALL, [S, K], False),
        (JoinPolicy.ALL, [P, F], False),  # can already never satisfy ALL
        (JoinPolicy.ALL, [P, S], None),
        (JoinPolicy.ANY_SUCCESS, [S, F], True),  # candidate A ok, B failed -> still evaluate
        (JoinPolicy.ANY_SUCCESS, [F, K], False),
        (JoinPolicy.ANY_SUCCESS, [P, F], None),
    ],
)
def test_join_semantics(join: JoinPolicy, deps: list[NodeStatus], expected: bool | None) -> None:
    assert should_run(join, deps) is expected


def test_retry_policy_backoff_is_capped() -> None:
    p = RetryPolicy(max_attempts=5, base_s=0.5, cap_s=8.0)
    assert [p.max_delay(a) for a in range(1, 7)] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    with pytest.raises(ValueError, match="invalid retry policy"):
        RetryPolicy(max_attempts=0)


@pytest.mark.parametrize(
    ("exc", "retryable"),
    [
        (ProviderTimeout("t"), True),
        (WorkerCrashed("segfault"), True),
        (ProviderClientError("400"), False),
        (WorkerOutputInvalid("bad json"), False),
        (ValueError("bug"), False),
        (KeyError("bug"), False),
    ],
)
def test_retry_classification(exc: BaseException, retryable: bool) -> None:
    assert RetryPolicy().classify(exc) is retryable
