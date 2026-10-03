"""DAG definitions (ARCHITECTURE §3). The scheduler arrives in Phase 3. This module freezes
the node model and the graph validation it depends on.

This is a clean-room redesign of Evoco's ``orchestrator/dag.py``. It fixes these issues:
- unknown dependencies and cycles are rejected when the graph is built, not left to hang;
- SKIPPED is its own status and is never counted as FAILED;
- results flow only along declared edges, never "everything completed so far";
- ``join=ANY_SUCCESS`` allows a fan-in to survive one failed branch, so a failed
  candidate A does not kill the job.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kinesis.errors import is_retryable


class JoinPolicy(StrEnum):
    ALL = "ALL"  # run only if every dependency SUCCEEDED, otherwise SKIPPED
    ANY_SUCCESS = "ANY_SUCCESS"  # run if at least one dependency SUCCEEDED


class NodeStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with full jitter. The delay for attempt n (1-based) is
    uniform(0, min(cap_s, base_s * 2**(n-1))). Only errors classified as retryable are
    retried."""

    max_attempts: int = 1
    base_s: float = 0.5
    cap_s: float = 8.0
    classify: Callable[[BaseException], bool] = is_retryable

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.base_s < 0 or self.cap_s < self.base_s:
            raise ValueError("invalid retry policy")

    def max_delay(self, attempt: int) -> float:
        return min(self.cap_s, self.base_s * 2.0 ** (attempt - 1))


NO_RETRY = RetryPolicy()

NodeFn = Callable[[Mapping[str, Any]], Awaitable[Any]]
"""Receives ``{dep_id: dep_result}`` for its SUCCEEDED dependencies only."""


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    fn: NodeFn
    deps: tuple[str, ...] = ()
    retry: RetryPolicy = NO_RETRY
    timeout_s: float = 60.0
    join: JoinPolicy = JoinPolicy.ALL
    cache_key: Callable[[Mapping[str, Any]], str] | None = None
    tags: Mapping[str, str] = field(default_factory=dict)  # e.g. {"candidate": "A"}


class DagDefinitionError(ValueError):
    pass


def validate_graph(nodes: Sequence[Node]) -> list[str]:
    """Validate the graph and return a deterministic topological order.

    Raises ``DagDefinitionError`` for duplicate ids, unknown dependencies, self-loops or
    cycles. Ties are broken by declaration order, so the result is stable.
    """
    index = {n.id: i for i, n in enumerate(nodes)}
    if len(index) != len(nodes):
        dupes = sorted(i for i, c in Counter(n.id for n in nodes).items() if c > 1)
        raise DagDefinitionError(f"duplicate node ids: {dupes}")
    for n in nodes:
        if n.timeout_s <= 0:
            raise DagDefinitionError(f"{n.id}: timeout_s must be > 0")
        for d in n.deps:
            if d == n.id:
                raise DagDefinitionError(f"{n.id}: depends on itself")
            if d not in index:
                raise DagDefinitionError(f"{n.id}: unknown dependency {d!r}")
        if n.join is JoinPolicy.ANY_SUCCESS and not n.deps:
            raise DagDefinitionError(f"{n.id}: ANY_SUCCESS requires dependencies")

    remaining = {n.id: set(n.deps) for n in nodes}
    order: list[str] = []
    while remaining:
        ready = sorted((i for i, deps in remaining.items() if not deps), key=index.__getitem__)
        if not ready:
            raise DagDefinitionError(f"cycle among: {sorted(remaining)}")
        for i in ready:
            order.append(i)
            del remaining[i]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


def should_run(join: JoinPolicy, dep_statuses: Sequence[NodeStatus]) -> bool | None:
    """Decide whether a node can run, given its dependency statuses.

    Returns True to run, False to skip, or None when it is too early to decide.
    """
    if any(s in (NodeStatus.PENDING, NodeStatus.RUNNING) for s in dep_statuses):
        if join is JoinPolicy.ALL and any(
            s in (NodeStatus.FAILED, NodeStatus.SKIPPED, NodeStatus.CANCELLED) for s in dep_statuses
        ):
            return False  # it can already never satisfy ALL
        return None
    if join is JoinPolicy.ALL:
        return all(s is NodeStatus.SUCCEEDED for s in dep_statuses)
    return any(s is NodeStatus.SUCCEEDED for s in dep_statuses)
