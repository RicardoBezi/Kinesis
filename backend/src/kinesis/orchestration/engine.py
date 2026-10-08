"""DAG scheduler (ARCHITECTURE §3, "Engine semantics"). Clean-room; no Evoco code.

- A ready set plus ``asyncio.wait(FIRST_COMPLETED)``: independent nodes run concurrently.
- Each node receives ``{dep_id: value}`` for its SUCCEEDED dependencies only.
- ``join=ALL`` with a failed or skipped dependency marks the node SKIPPED (never FAILED);
  ``join=ANY_SUCCESS`` runs when at least one dependency succeeded.
- A node may raise ``SkipNode`` to end its branch without failing (for example "no defect").
- Retries follow the node's ``RetryPolicy``: full-jitter backoff, only for errors the policy
  classifies as retryable, honouring ``retry_after_s`` (HTTP 429) when an error carries it.
- Per-attempt timeouts raise ``NodeTimeout`` (retryable) unless the node raised its own.
- Cancelling ``run`` cancels every child task in ``finally`` and re-raises.
- An exception inside the event callback is logged and never aborts the DAG.
- Optional result caching: nodes with ``cache_key`` and ``codec`` are looked up first.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kinesis.errors import KinesisError
from kinesis.orchestration.cache import Cache
from kinesis.orchestration.dag import Node, NodeStatus, should_run, validate_graph
from kinesis.schemas.common import ErrorCode

log = logging.getLogger("kinesis.dag")


class SkipNode(Exception):
    """Raised by a node to end its branch as SKIPPED (not FAILED)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class NodeTimeout(KinesisError):
    """A node attempt exceeded ``Node.timeout_s``."""

    code = ErrorCode.INTERNAL
    retryable = True


class NodeEventKind(StrEnum):
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    RETRY = "RETRY"
    CACHE_HIT = "CACHE_HIT"


@dataclass(frozen=True, slots=True)
class NodeEvent:
    kind: NodeEventKind
    node: Node
    attempt: int = 1
    error: BaseException | None = None
    delay_s: float | None = None
    message: str = ""


EventCallback = Callable[[NodeEvent], Awaitable[None] | None]


@dataclass(slots=True)
class NodeOutcome:
    status: NodeStatus
    value: Any = None
    error: BaseException | None = None
    attempts: int = 0
    cached: bool = False
    elapsed_ms: int = 0
    skip_reason: str | None = None


@dataclass(slots=True)
class DagResult:
    outcomes: dict[str, NodeOutcome] = field(default_factory=dict)

    def status(self, node_id: str) -> NodeStatus:
        return self.outcomes[node_id].status

    def value(self, node_id: str) -> Any:
        return self.outcomes[node_id].value

    def succeeded(self, node_id: str) -> bool:
        return self.outcomes[node_id].status is NodeStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class Codec:
    """JSON (de)serialization for cached node values. Nothing is pickled (ADR 0005)."""

    encode: Callable[[Any], str]
    decode: Callable[[str], Any]


class DagRunner:
    def __init__(
        self,
        *,
        on_event: EventCallback | None = None,
        cache: Cache | None = None,
        codecs: Mapping[str, Codec] | None = None,
        cache_ttl_s: int = 24 * 3600,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._on_event = on_event
        self._cache = cache
        self._codecs = dict(codecs or {})
        self._cache_ttl_s = cache_ttl_s
        self._sleep = sleep
        self._jitter = jitter

    async def run(self, nodes: Sequence[Node]) -> DagResult:
        order = validate_graph(nodes)
        by_id = {n.id: n for n in nodes}
        result = DagResult({nid: NodeOutcome(NodeStatus.PENDING) for nid in order})
        running: dict[asyncio.Task[NodeOutcome], str] = {}
        try:
            while True:
                await self._schedule(order, by_id, result, running)
                if not running:
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    node_id = running.pop(task)
                    result.outcomes[node_id] = task.result()
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
                for node_id in running.values():
                    result.outcomes[node_id].status = NodeStatus.CANCELLED
        return result

    async def _schedule(
        self,
        order: list[str],
        by_id: dict[str, Node],
        result: DagResult,
        running: dict[asyncio.Task[NodeOutcome], str],
    ) -> None:
        progressed = True
        while progressed:
            progressed = False
            for node_id in order:
                outcome = result.outcomes[node_id]
                if outcome.status is not NodeStatus.PENDING:
                    continue
                node = by_id[node_id]
                decision = should_run(node.join, [result.status(d) for d in node.deps])
                if decision is None:
                    continue
                progressed = True
                if not decision:
                    outcome.status = NodeStatus.SKIPPED
                    outcome.skip_reason = "dependency did not succeed"
                    await self._emit(NodeEvent(NodeEventKind.SKIPPED, node, message="upstream"))
                    continue
                inputs = {d: result.value(d) for d in node.deps if result.succeeded(d)}
                outcome.status = NodeStatus.RUNNING
                task = asyncio.create_task(self._run_node(node, inputs), name=f"dag:{node_id}")
                running[task] = node_id

    async def _run_node(self, node: Node, inputs: Mapping[str, Any]) -> NodeOutcome:
        started = time.monotonic()

        def done(outcome: NodeOutcome) -> NodeOutcome:
            outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
            return outcome

        key = node.cache_key(inputs) if node.cache_key and self._cache is not None else None
        codec = self._codecs.get(node.id)
        if key is not None and codec is not None and self._cache is not None:
            hit = await self._cache.get(key)
            if hit is not None:
                await self._emit(NodeEvent(NodeEventKind.CACHE_HIT, node))
                return done(NodeOutcome(NodeStatus.SUCCEEDED, codec.decode(hit), cached=True))

        policy = node.retry
        attempt = 0
        while True:
            attempt += 1
            await self._emit(NodeEvent(NodeEventKind.STARTED, node, attempt))
            try:
                value = await asyncio.wait_for(node.fn(inputs), node.timeout_s)
            except SkipNode as skip:
                await self._emit(
                    NodeEvent(NodeEventKind.SKIPPED, node, attempt, message=skip.reason)
                )
                return done(
                    NodeOutcome(NodeStatus.SKIPPED, attempts=attempt, skip_reason=skip.reason)
                )
            except TimeoutError as exc:
                error: Exception = NodeTimeout(
                    f"{node.id}: exceeded {node.timeout_s:g} s", node=node.id
                )
                error.__cause__ = exc
            except Exception as exc:  # classified below; never swallowed silently
                error = exc
            else:
                if key is not None and codec is not None and self._cache is not None:
                    await self._cache.set(key, codec.encode(value), ttl_s=self._cache_ttl_s)
                await self._emit(NodeEvent(NodeEventKind.SUCCEEDED, node, attempt))
                return done(NodeOutcome(NodeStatus.SUCCEEDED, value, attempts=attempt))

            if attempt < policy.max_attempts and policy.classify(error):
                delay = self._jitter() * policy.max_delay(attempt)
                retry_after = getattr(error, "retry_after_s", None)
                if isinstance(retry_after, int | float):
                    delay = max(delay, float(retry_after))
                await self._emit(
                    NodeEvent(NodeEventKind.RETRY, node, attempt, error=error, delay_s=delay)
                )
                await self._sleep(delay)
                continue
            await self._emit(NodeEvent(NodeEventKind.FAILED, node, attempt, error=error))
            return done(NodeOutcome(NodeStatus.FAILED, error=error, attempts=attempt))

    async def _emit(self, event: NodeEvent) -> None:
        if self._on_event is None:
            return
        try:
            maybe = self._on_event(event)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            log.exception("dag.event_callback_failed", extra={"node": event.node.id})


__all__ = [
    "Codec",
    "DagResult",
    "DagRunner",
    "EventCallback",
    "NodeEvent",
    "NodeEventKind",
    "NodeOutcome",
    "NodeTimeout",
    "SkipNode",
]
