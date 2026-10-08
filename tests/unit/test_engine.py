"""DAG engine, circuit breaker and caches (ARCHITECTURE §3, FAILURE_MODES)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from kinesis.errors import (
    CircuitOpen,
    ProviderClientError,
    ProviderRateLimited,
    ProviderServerError,
    WorkerCrashed,
    WorkerOutputInvalid,
)
from kinesis.orchestration.breaker import BreakerState, CircuitBreaker
from kinesis.orchestration.caches import MemoryCache, RedisCache
from kinesis.orchestration.dag import JoinPolicy, Node, NodeStatus, RetryPolicy
from kinesis.orchestration.engine import (
    Codec,
    DagRunner,
    NodeEvent,
    NodeEventKind,
    NodeTimeout,
    SkipNode,
)


class Recorder:
    def __init__(self) -> None:
        self.events: list[NodeEvent] = []
        self.sleeps: list[float] = []

    def __call__(self, event: NodeEvent) -> None:
        self.events.append(event)

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)

    def kinds(self, node: str) -> list[NodeEventKind]:
        return [e.kind for e in self.events if e.node.id == node]


def runner(rec: Recorder | None = None, **kw: Any) -> DagRunner:
    rec = rec or Recorder()
    return DagRunner(on_event=rec, sleep=rec.sleep, jitter=lambda: 1.0, **kw)


def const(value: Any) -> Any:
    async def fn(_: Mapping[str, Any]) -> Any:
        return value

    return fn


def fail(exc: BaseException) -> Any:
    async def fn(_: Mapping[str, Any]) -> Any:
        raise exc

    return fn


async def test_results_flow_only_along_declared_edges() -> None:
    seen: dict[str, Mapping[str, Any]] = {}

    def capture(name: str, value: Any) -> Any:
        async def fn(inputs: Mapping[str, Any]) -> Any:
            seen[name] = dict(inputs)
            return value

        return fn

    nodes = [
        Node("a", capture("a", 1)),
        Node("b", capture("b", 2), deps=("a",)),
        Node("c", capture("c", 3), deps=("b",)),
    ]
    result = await runner().run(nodes)
    assert seen["c"] == {"b": 2}  # not {"a": 1, "b": 2}
    assert [result.value(n) for n in "abc"] == [1, 2, 3]


async def test_independent_nodes_run_concurrently() -> None:
    gate = asyncio.Event()
    started: list[str] = []

    def branch(name: str) -> Any:
        async def fn(_: Mapping[str, Any]) -> str:
            started.append(name)
            if len(started) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 1.0)  # deadlocks unless both run at once
            return name

        return fn

    nodes = [
        Node("root", const(0)),
        Node("A", branch("A"), ("root",)),
        Node("B", branch("B"), ("root",)),
    ]
    result = await runner().run(nodes)
    assert result.succeeded("A")
    assert result.succeeded("B")


async def test_failed_dependency_skips_all_join_and_any_success_survives() -> None:
    rec = Recorder()
    nodes = [
        Node("p", const(0)),
        Node("a1", fail(ValueError("boom")), ("p",)),
        Node("a2", const("x"), ("a1",)),
        Node("b1", const("b"), ("p",)),
        Node("join", const("ok"), ("a2", "b1"), join=JoinPolicy.ANY_SUCCESS),
    ]
    result = await runner(rec).run(nodes)
    assert result.status("a1") is NodeStatus.FAILED
    assert result.status("a2") is NodeStatus.SKIPPED
    assert result.status("join") is NodeStatus.SUCCEEDED
    assert rec.kinds("a2") == [NodeEventKind.SKIPPED]


async def test_any_success_skipped_when_every_branch_fails() -> None:
    nodes = [
        Node("a", fail(ValueError("a"))),
        Node("b", fail(ValueError("b"))),
        Node("join", const(1), ("a", "b"), join=JoinPolicy.ANY_SUCCESS),
    ]
    result = await runner().run(nodes)
    assert result.status("join") is NodeStatus.SKIPPED


async def test_skip_node_ends_branch_without_failure() -> None:
    nodes = [Node("gate", fail(SkipNode("no defect"))), Node("after", const(1), ("gate",))]
    result = await runner().run(nodes)
    assert result.status("gate") is NodeStatus.SKIPPED
    assert result.outcomes["gate"].skip_reason == "no defect"
    assert result.status("after") is NodeStatus.SKIPPED


async def test_retryable_errors_retry_with_capped_backoff() -> None:
    rec = Recorder()
    calls = 0

    async def flaky(_: Mapping[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise WorkerCrashed("exit 139")
        return "ok"

    policy = RetryPolicy(max_attempts=3, base_s=0.5, cap_s=0.75)
    result = await runner(rec).run([Node("w", flaky, retry=policy)])
    assert result.value("w") == "ok"
    assert result.outcomes["w"].attempts == 3
    assert rec.sleeps == [0.5, 0.75]  # jitter=1.0 -> the cap applies on attempt 2
    assert rec.kinds("w").count(NodeEventKind.RETRY) == 2


async def test_non_retryable_error_fails_immediately() -> None:
    rec = Recorder()
    policy = RetryPolicy(max_attempts=3)
    result = await runner(rec).run([Node("w", fail(WorkerOutputInvalid("bad json")), retry=policy)])
    assert result.status("w") is NodeStatus.FAILED
    assert result.outcomes["w"].attempts == 1
    assert rec.sleeps == []


async def test_retry_after_is_honoured() -> None:
    rec = Recorder()
    calls = 0

    async def limited(_: Mapping[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderRateLimited("429", retry_after_s=7.0)
        return "ok"

    result = await runner(rec).run([Node("m", limited, retry=RetryPolicy(max_attempts=2))])
    assert result.value("m") == "ok"
    assert rec.sleeps == [7.0]


async def test_timeout_is_retryable_node_timeout() -> None:
    async def slow(_: Mapping[str, Any]) -> None:
        await asyncio.sleep(10)

    result = await runner().run(
        [Node("s", slow, timeout_s=0.01, retry=RetryPolicy(max_attempts=2))]
    )
    outcome = result.outcomes["s"]
    assert outcome.status is NodeStatus.FAILED
    assert isinstance(outcome.error, NodeTimeout)
    assert outcome.attempts == 2


async def test_cancel_cancels_child_tasks() -> None:
    cancelled: list[str] = []
    started = asyncio.Event()

    async def long(_: Mapping[str, Any]) -> None:
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append("long")
            raise

    task = asyncio.create_task(runner().run([Node("long", long, timeout_s=60)]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == ["long"]


async def test_event_callback_exception_does_not_abort() -> None:
    def broken(_: NodeEvent) -> None:
        raise RuntimeError("callback bug")

    result = await DagRunner(on_event=broken).run(
        [Node("a", const(1)), Node("b", const(2), ("a",))]
    )
    assert result.value("b") == 2


async def test_async_callback_is_awaited() -> None:
    seen: list[str] = []

    async def cb(event: NodeEvent) -> None:
        seen.append(f"{event.node.id}:{event.kind}")

    await DagRunner(on_event=cb).run([Node("a", const(1))])
    assert seen == ["a:STARTED", "a:SUCCEEDED"]


async def test_cache_hit_skips_execution() -> None:
    calls = 0

    async def compute(_: Mapping[str, Any]) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"v": 42}

    cache = MemoryCache()
    codec = Codec(encode=json.dumps, decode=json.loads)
    node = Node("c", compute, cache_key=lambda _: "kinesis:test:k")
    first = await runner(cache=cache, codecs={"c": codec}).run([node])
    rec = Recorder()
    second = await runner(rec, cache=cache, codecs={"c": codec}).run([node])
    assert first.value("c") == second.value("c") == {"v": 42}
    assert calls == 1
    assert second.outcomes["c"].cached
    assert rec.kinds("c") == [NodeEventKind.CACHE_HIT]


# ------------------------------------------------------------------ breaker


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def _boom() -> None:
    raise ProviderServerError("503")


async def _ok() -> str:
    return "ok"


async def test_breaker_opens_after_threshold_and_fails_fast() -> None:
    clock = Clock()
    br = CircuitBreaker("tf", failure_threshold=3, recovery_s=30, clock=clock)
    for _ in range(3):
        with pytest.raises(ProviderServerError):
            await br.call(_boom)
    assert br.state is BreakerState.OPEN
    with pytest.raises(CircuitOpen) as err:
        await br.call(_ok)
    assert err.value.retry_after_s == pytest.approx(30)


async def test_breaker_half_open_probe_closes_or_reopens() -> None:
    clock = Clock()
    br = CircuitBreaker("tf", failure_threshold=1, recovery_s=10, clock=clock)
    with pytest.raises(ProviderServerError):
        await br.call(_boom)
    clock.t = 10
    with pytest.raises(ProviderServerError):
        await br.call(_boom)  # failed probe re-opens
    assert br.state is BreakerState.OPEN
    clock.t = 20
    assert await br.call(_ok) == "ok"
    assert br.state is BreakerState.CLOSED


async def test_breaker_admits_one_probe_at_a_time() -> None:
    clock = Clock()
    br = CircuitBreaker("tf", failure_threshold=1, recovery_s=0, clock=clock)
    with pytest.raises(ProviderServerError):
        await br.call(_boom)
    release = asyncio.Event()

    async def slow_ok() -> str:
        await release.wait()
        return "ok"

    probe = asyncio.create_task(br.call(slow_ok))
    await asyncio.sleep(0)
    with pytest.raises(CircuitOpen):
        await br.call(_ok)
    release.set()
    assert await probe == "ok"
    assert br.state is BreakerState.CLOSED


async def test_client_errors_are_neutral_and_release_the_probe() -> None:
    clock = Clock()
    br = CircuitBreaker("tf", failure_threshold=1, recovery_s=0, clock=clock)

    async def bad_request() -> None:
        raise ProviderClientError("400")

    for _ in range(3):
        with pytest.raises(ProviderClientError):
            await br.call(bad_request)
    assert br.state is BreakerState.CLOSED
    with pytest.raises(ProviderServerError):
        await br.call(_boom)
    with pytest.raises(ProviderClientError):
        await br.call(bad_request)  # neutral probe
    assert await br.call(_ok) == "ok"  # the probe slot was released


async def test_cancelled_probe_releases_its_slot() -> None:
    br = CircuitBreaker("tf", failure_threshold=1, recovery_s=0)
    with pytest.raises(ProviderServerError):
        await br.call(_boom)

    async def hang() -> None:
        await asyncio.sleep(10)

    probe = asyncio.create_task(br.call(hang))
    await asyncio.sleep(0)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert await br.call(_ok) == "ok"


# ------------------------------------------------------------------ caches


async def test_memory_cache_lru_and_ttl() -> None:
    clock = Clock()
    cache = MemoryCache(max_entries=2, clock=clock)
    await cache.set("a", "1", ttl_s=10)
    await cache.set("b", "2", ttl_s=10)
    assert await cache.get("a") == "1"  # a becomes most recent
    await cache.set("c", "3", ttl_s=10)
    assert await cache.get("b") is None
    assert len(cache) == 2
    clock.t = 11
    assert await cache.get("a") is None
    assert await cache.set_if_absent("k", "v", ttl_s=5)
    assert not await cache.set_if_absent("k", "w", ttl_s=5)
    await cache.delete("k")
    assert await cache.get("k") is None


async def test_redis_cache_round_trip() -> None:
    cache = RedisCache(fakeredis.FakeAsyncRedis(decode_responses=True))
    await cache.set("k", "v", ttl_s=60)
    assert await cache.get("k") == "v"
    assert await cache.set_if_absent("n", "1", ttl_s=60)
    assert not await cache.set_if_absent("n", "2", ttl_s=60)
    await cache.delete("k")
    assert await cache.get("k") is None
    assert cache.degraded_calls == 0


class BrokenRedis:
    async def get(self, *_: Any, **__: Any) -> None:
        raise RedisConnectionError("down")

    set = delete = get


async def test_redis_unavailable_degrades_to_memory(caplog: pytest.LogCaptureFixture) -> None:
    cache = RedisCache(BrokenRedis())
    with caplog.at_level("WARNING", logger="kinesis.cache"):
        await cache.set("k", "v", ttl_s=60)
        assert await cache.get("k") == "v"  # served by the fallback
        assert await cache.set_if_absent("k", "w", ttl_s=60) is False
    assert cache.degraded_calls == 3
    assert [r.message for r in caplog.records].count("cache.degraded") == 1  # rate-limited


# ------------------------------------------------------------------ correlation


async def test_log_records_carry_node_candidate_and_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from kinesis.observability.context import CorrelationFilter, bind

    logger = logging.getLogger("kinesis.test")
    calls = 0

    async def noisy(_: Mapping[str, Any]) -> None:
        nonlocal calls
        calls += 1
        logger.warning("inside")
        if calls == 1:
            raise WorkerCrashed("first attempt")

    caplog.handler.addFilter(CorrelationFilter())
    node = Node("apply_render_A", noisy, retry=RetryPolicy(max_attempts=2), tags={"candidate": "A"})
    with caplog.at_level("WARNING", logger="kinesis.test"), bind(job_id="job_corr01"):
        await runner().run([node])
    records = [r for r in caplog.records if r.getMessage() == "inside"]
    assert [(r.job_id, r.node, r.candidate, r.attempt) for r in records] == [  # type: ignore[attr-defined]
        ("job_corr01", "apply_render_A", "A", 1),
        ("job_corr01", "apply_render_A", "A", 2),
    ]


def test_json_formatter_includes_correlation_fields() -> None:
    import logging

    from kinesis.observability.context import CorrelationFilter, JsonFormatter, bind

    record = logging.LogRecord("kinesis.x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    with bind(job_id="job_json01", node="extract_scope"):
        CorrelationFilter().filter(record)
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "hello world"
    assert payload["job_id"] == "job_json01"
    assert payload["node"] == "extract_scope"
    assert "candidate" not in payload  # unset fields are omitted
