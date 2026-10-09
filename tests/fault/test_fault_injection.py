"""Fault-injection scenarios (docs/FAILURE_MODES.md).

Every scenario drives the real ``JobService`` and repair DAG with a ``FakeJobRunner`` and a
``MockProvider``. Each asserts the retry behaviour, the isolation scope and the error code the
user sees. Backoff sleeps are recorded, not slept.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from conftest import FAKE_BLEND, chunks, job_request, run_job

from kinesis.errors import ProviderRateLimited, ProviderServerError, ProviderTimeout
from kinesis.orchestration.breaker import BreakerState
from kinesis.orchestration.caches import RedisCache
from kinesis.providers.mock import MockProvider
from kinesis.schemas import (
    CandidateLabel,
    CandidateStatus,
    EvaluatorStatus,
    JobEvent,
    JobEventType,
    JobStatus,
    PlanSource,
    RepairJob,
)
from kinesis.schemas.common import ErrorCode
from kinesis.schemas.worker import WorkerCommand
from kinesis.testing.fake_runner import FakeJobRunner, Fault
from kinesis.testing.synthetic import foot_slide_v1

pytestmark = pytest.mark.fault


def candidate(job: RepairJob, label: str) -> Any:
    return next(c for c in job.candidates if c.label is CandidateLabel(label))


async def events(service: Any, job: RepairJob) -> list[JobEvent]:
    return list(await service.store.list_events(job.job_id, after_seq=0, limit=500))


# ------------------------------------------------------------------ provider faults (#1-#6)


async def test_model_timeout_retried_then_fallback_plan(make_service: Any) -> None:
    provider = MockProvider(plans=[ProviderTimeout("read timeout")] * 3)
    service = make_service(provider=provider)
    job = await run_job(service)
    assert provider.plan_calls == 3
    assert job.plan is not None
    assert job.plan.plan_source is PlanSource.FALLBACK
    assert "PROVIDER_TIMEOUT" in (job.plan.fallback_reason or "")
    rejected = [e for e in await events(service, job) if e.type is JobEventType.PLAN_REJECTED]
    assert rejected
    assert rejected[0].data["code"] == "PROVIDER_TIMEOUT"
    assert job.status is JobStatus.AWAITING_DECISION


async def test_model_429_respects_retry_after(make_service: Any) -> None:
    provider = MockProvider(plans=[ProviderRateLimited("429", retry_after_s=12.0)])
    service = make_service(provider=provider)
    job = await run_job(service)
    assert provider.plan_calls == 2
    assert 12.0 in service.sleep_recorder.calls
    assert job.plan is not None
    assert job.plan.plan_source is PlanSource.MODEL


async def test_breaker_opens_after_threshold_and_fails_fast(make_service: Any) -> None:
    provider = MockProvider(plans=[ProviderServerError("503")] * 100)
    service = make_service(provider=provider)
    first = await run_job(service, key="key-00000001")
    second = await run_job(service, key="key-00000002")
    assert provider.plan_calls == 5  # 3 + 2, then the breaker opened mid-retry
    assert service.planner_breaker.state is BreakerState.OPEN
    third = await run_job(service, key="key-00000003")
    assert provider.plan_calls == 5  # failed fast, no call made
    for job in (first, second, third):
        assert job.plan is not None
        assert job.plan.plan_source is PlanSource.FALLBACK
        assert job.status is JobStatus.AWAITING_DECISION
    assert third.plan is not None
    assert "circuit open" in (third.plan.fallback_reason or "")


async def test_malformed_model_plan_uses_fallback(make_service: Any) -> None:
    provider = MockProvider(plans=["{not json"])
    service = make_service(provider=provider)
    job = await run_job(service)
    assert job.plan is not None
    assert job.plan.plan_source is PlanSource.FALLBACK
    rejected = [e for e in await events(service, job) if e.type is JobEventType.PLAN_REJECTED]
    assert rejected[0].data["code"] == "PLAN_INVALID"


async def test_visual_evaluation_failure_degrades(make_service: Any) -> None:
    provider = MockProvider(evaluations=[ProviderServerError("503")] * 3)
    service = make_service(provider=provider)
    job = await run_job(service)
    assert job.evaluation is not None
    assert job.evaluation.evaluator_status is EvaluatorStatus.DEGRADED
    assert len(job.evaluation.visual) == 1  # the other candidate was judged
    assert job.evaluation.objective_ranking
    assert provider.eval_frame_counts
    assert all(n <= 10 for n in provider.eval_frame_counts)  # S4: at most 10 images per call


# ------------------------------------------------------------------ worker faults (#8-#14)


async def test_blender_subprocess_crash_retried_once(make_service: Any) -> None:
    runner = FakeJobRunner({"apply_a": [Fault.CRASH], "extract_scope": [Fault.CRASH] * 2})
    service = make_service(runner=runner)
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION
    assert candidate(job, "A").status is CandidateStatus.SUCCEEDED
    retries = [e for e in await events(service, job) if e.type is JobEventType.NODE_RETRY]
    assert {e.node for e in retries} == {"apply_render_A", "extract_scope"}


async def test_extract_crash_exhausted_fails_job(make_service: Any) -> None:
    service = make_service(runner=FakeJobRunner({"extract_scope": [Fault.CRASH] * 3}))
    job = await run_job(service)
    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert job.error.code is ErrorCode.WORKER_CRASHED
    assert job.error.node == "extract_scope"


async def test_candidate_a_fails_candidate_b_succeeds_awaiting_decision(make_service: Any) -> None:
    service = make_service(runner=FakeJobRunner({"apply_a": [Fault.CRASH] * 2}))
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION
    a, b = candidate(job, "A"), candidate(job, "B")
    assert a.status is CandidateStatus.FAILED
    assert a.error is not None
    assert a.error.code is ErrorCode.WORKER_CRASHED
    assert "apply_render_A" in a.error.message
    assert "(attempt 2/2)" in a.error.message
    assert "candidate B unaffected" in a.error.message
    assert b.status is CandidateStatus.SUCCEEDED
    assert job.evaluation is not None
    assert [r.label for r in job.evaluation.objective_ranking] == [CandidateLabel.B]


async def test_both_candidates_fail_job_failed_no_viable_candidate(make_service: Any) -> None:
    runner = FakeJobRunner({"apply_a": [Fault.CRASH] * 2, "apply_b": [Fault.CRASH] * 2})
    service = make_service(runner=runner)
    job = await run_job(service)
    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert job.error.code is ErrorCode.NO_VIABLE_CANDIDATE


async def test_render_timeout_isolated_to_candidate(make_service: Any) -> None:
    service = make_service(runner=FakeJobRunner({"apply_b": [Fault.TIMEOUT] * 2}))
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION
    b = candidate(job, "B")
    assert b.status is CandidateStatus.FAILED
    assert b.error is not None
    assert b.error.code is ErrorCode.WORKER_TIMEOUT
    assert candidate(job, "A").status is CandidateStatus.SUCCEEDED


async def test_malformed_worker_result_not_retried(make_service: Any) -> None:
    runner = FakeJobRunner({"apply_a": [Fault.MALFORMED]})
    service = make_service(runner=runner)
    job = await run_job(service)
    a = candidate(job, "A")
    assert a.status is CandidateStatus.FAILED
    assert a.error is not None
    assert a.error.code is ErrorCode.WORKER_OUTPUT_INVALID
    assert sum(1 for c in runner.calls if c.node == "apply_a") == 1


async def test_original_hash_change_is_a_bug_alarm(
    make_service: Any, caplog: pytest.LogCaptureFixture
) -> None:
    service = make_service(runner=FakeJobRunner({"apply_b": [Fault.HASH_MISMATCH]}))
    with caplog.at_level("ERROR", logger="kinesis.pipeline"):
        job = await run_job(service)
    b = candidate(job, "B")
    assert b.status is CandidateStatus.FAILED
    assert b.error is not None
    assert b.error.code is ErrorCode.WORKER_OUTPUT_INVALID
    assert any(r.message == "bug_alarm.original_action_modified" for r in caplog.records)


async def test_serverless_job_failure_surfaces_error(make_service: Any, tmp_path: Any) -> None:
    """FAILURE_MODES #19: a Nebius job that ends ERROR fails the node with the platform's reason."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from kinesis.jobs.nebius.api import NebiusJobsClient
    from kinesis.jobs.nebius.auth import NebiusTokenProvider, ServiceAccountKey
    from kinesis.jobs.nebius.config import NebiusJobConfig, SpendGuard
    from kinesis.jobs.nebius.runner import NebiusJobRunner
    from kinesis.testing.fake_nebius import FakeNebius, Outcome

    pem = tmp_path / "k.pem"
    pem.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    fake = FakeNebius(
        outcomes=[Outcome.NORMAL] + [Outcome.ERROR] * 3
    )  # inspect ok, extract x3 fail
    transport = fake.transport()
    tokens = NebiusTokenProvider(ServiceAccountKey("sa", "kid", pem), transport=transport)

    async def no_sleep(_: float) -> None:
        return None

    runner = NebiusJobRunner(
        NebiusJobsClient(tokens, transport=transport),
        fake.store,
        NebiusJobConfig(project_id="p", image="img", bucket=fake.store.bucket),
        SpendGuard(tmp_path / "spend.json", 5.0, None),
        sleep=no_sleep,
    )
    job = await run_job(make_service(runner=runner))
    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert job.error.code is ErrorCode.WORKER_CRASHED
    assert job.error.node == "extract_scope"
    assert "quota exceeded" in job.error.message


# ------------------------------------------------------------------ infrastructure (#16, #20, #25)


class BrokenRedis:
    async def get(self, *_: Any, **__: Any) -> None:
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("down")

    set = delete = get


async def test_redis_unavailable_degrades_to_memory_cache(make_service: Any) -> None:
    cache = RedisCache(BrokenRedis())
    service = make_service(cache=cache)
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION
    assert cache.degraded_calls > 0


async def test_extract_is_cached_across_jobs(make_service: Any) -> None:
    from kinesis.orchestration.caches import MemoryCache

    runner = FakeJobRunner()
    service = make_service(runner=runner, cache=MemoryCache())
    scene = await service.upload_scene(chunks(FAKE_BLEND))
    for key in ("key-00000001", "key-00000002"):
        await service.create_job(job_request(scene_id=scene.scene_id), key)
        await service.wait_idle()
    assert sum(1 for c in runner.calls if c.command is WorkerCommand.EXTRACT) == 1


async def test_duplicate_status_event_deduplicated(make_service: Any) -> None:
    service = make_service()
    job = await run_job(service)
    before = job.last_event_seq
    event = JobEvent(seq=1, job_id=job.job_id, ts=job.updated_at, type=JobEventType.NODE_SUCCEEDED)
    first = await service.store.append_event(event, dedupe_key="callback-42")
    second = await service.store.append_event(event, dedupe_key="callback-42")
    assert first.seq == second.seq == before + 1
    assert (await service.get_job(job.job_id)).last_event_seq == before + 1


async def test_cancel_mid_run_cancels_child_tasks(make_service: Any) -> None:
    runner = FakeJobRunner({"extract_scope": [Fault.HANG]})
    service = make_service(runner=runner)
    scene = await service.upload_scene(chunks(FAKE_BLEND))
    job, _ = await service.create_job(job_request(scene_id=scene.scene_id), "key-00000001")
    for _ in range(200):
        if runner.in_flight:
            break
        await asyncio.sleep(0.005)
    cancelled = await service.cancel(job.job_id)
    assert cancelled.status is JobStatus.CANCELLED
    assert runner.cancelled == ["extract_scope"]
    assert runner.in_flight == 0


async def test_event_callback_exception_does_not_abort_dag(make_service: Any) -> None:
    service = make_service()
    original = service.store.append_event

    async def flaky(event: JobEvent, **kw: Any) -> JobEvent:
        if event.type is JobEventType.NODE_STARTED:
            raise RuntimeError("event store hiccup")
        return await original(event, **kw)

    service.store.append_event = flaky
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION


# ------------------------------------------------------------------ pipeline behaviour


async def test_candidates_run_concurrently(make_service: Any) -> None:
    runner = FakeJobRunner(delay_s=0.05)
    service = make_service(runner=runner)
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION
    assert runner.max_concurrent(WorkerCommand.APPLY_RENDER) >= 2


async def test_happy_path_records_everything(make_service: Any) -> None:
    service = make_service(provider=MockProvider())
    job = await run_job(service)
    assert job.status is JobStatus.AWAITING_DECISION
    assert job.defect is not None
    assert job.defect.worst is not None
    assert 9.5 <= job.defect.worst.planted_displacement_cm <= 10.5
    assert job.plan is not None
    assert job.plan.plan_source is PlanSource.MODEL
    for c in job.candidates:
        assert c.status is CandidateStatus.SUCCEEDED
        assert c.metrics is not None
        assert c.metrics.slip_reduction_pct >= 80
        kinds = {a.kind.value for a in c.artifacts}
        assert kinds == {"CANDIDATE_BLEND", "PREVIEW_FRAMES", "CONTEXT_FRAMES", "METRICS_JSON"}
    assert {a.kind.value for a in job.original_artifacts} == {"PREVIEW_FRAMES", "CONTEXT_FRAMES"}
    assert job.evaluation is not None
    assert job.evaluation.evaluator_status is EvaluatorStatus.OK
    assert job.evaluation.recommended is CandidateLabel.B  # objective ranking (PHASE2.md)
    seqs = [e.seq for e in await events(service, job)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_null_provider_skips_visual_evaluation(make_service: Any) -> None:
    job = await run_job(make_service())
    assert job.evaluation is not None
    assert job.evaluation.evaluator_status is EvaluatorStatus.SKIPPED
    assert job.plan is not None
    assert job.plan.fallback_reason == "no model provider configured"


async def test_no_defect_completes_without_candidates(make_service: Any) -> None:
    service = make_service(runner=FakeJobRunner(scene=foot_slide_v1(defect=False)))
    job = await run_job(service)
    assert job.status is JobStatus.COMPLETED
    assert job.defect is not None
    assert job.defect.severity.value == "NONE"
    assert job.candidates == ()


async def test_restart_marks_running_jobs_failed(make_service: Any) -> None:
    runner = FakeJobRunner({"extract_scope": [Fault.HANG]})
    service = make_service(runner=runner)
    scene = await service.upload_scene(chunks(FAKE_BLEND))
    job, _ = await service.create_job(job_request(scene_id=scene.scene_id), "key-00000001")
    for _ in range(200):
        if runner.in_flight:
            break
        await asyncio.sleep(0.005)
    for task in list(service._tasks.values()):  # simulate the process dying
        task.cancel()
    await service.wait_idle()
    assert await service.recover() == 1
    failed = await service.get_job(job.job_id)
    assert failed.status is JobStatus.FAILED
    assert failed.error is not None
    assert "restart" in failed.error.message
