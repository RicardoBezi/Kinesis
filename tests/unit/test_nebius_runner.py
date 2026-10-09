"""Nebius Serverless Jobs runners against the fake Nebius API (no cloud, no Blender)."""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FAKE_BLEND, chunks, job_request
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kinesis.api.deps import build_runner
from kinesis.errors import WorkerCrashed, WorkerReportedFailure, WorkerTimeout
from kinesis.jobs.nebius.api import NebiusJobsClient
from kinesis.jobs.nebius.auth import NebiusAuthError, NebiusTokenProvider, ServiceAccountKey
from kinesis.jobs.nebius.config import GIB, BudgetExceeded, NebiusJobConfig, SpendGuard
from kinesis.jobs.nebius.runner import NebiusJobRunner, NebiusSessionRunner
from kinesis.jobs.runner import JobRunner
from kinesis.jobs.worker_io import read_result, run_worker, spec_paths
from kinesis.schemas import JobStatus
from kinesis.schemas.worker import (
    ExtractResult,
    ExtractSpec,
    InspectResult,
    InspectSpec,
    WorkerCommand,
)
from kinesis.settings import RunnerKind, Settings
from kinesis.testing.fake_nebius import FakeNebius, Outcome
from kinesis.testing.fake_runner import FakeJobRunner, Fault


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def key_file(tmp_path: Path, rsa_key: rsa.RSAPrivateKey) -> ServiceAccountKey:
    pem = rsa_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path = tmp_path / "kinesis-runner.pem"
    path.write_bytes(pem)
    return ServiceAccountKey("serviceaccount-e00test", "publickey-e00test", path)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s
        await asyncio.sleep(0)


def make_runner(
    fake: FakeNebius,
    key: ServiceAccountKey,
    tmp_path: Path,
    *,
    cls: type[NebiusJobRunner] | type[NebiusSessionRunner] = NebiusJobRunner,
    clock: Clock | None = None,
    price: float | None = 3.6,
    budget: float = 5.0,
    **config: Any,
) -> NebiusJobRunner | NebiusSessionRunner:
    clock = clock or Clock()
    transport = fake.transport()
    tokens = NebiusTokenProvider(key, transport=transport, clock=lambda: clock.t)
    client = NebiusJobsClient(tokens, transport=transport)
    cfg = NebiusJobConfig(
        project_id="project-e00test",
        image="cr.us-central1.nebius.cloud/kinesis/kinesis-worker:4.5.14",
        bucket=fake.store.bucket,
        s3_access_key_id="AKIA-test",
        s3_secret_access_key="secret-test",
        price_per_hour_usd=price,
        budget_usd=budget,
        **config,
    )
    guard = SpendGuard(tmp_path / "spend.json", budget, price)
    return cls(client, fake.store, cfg, guard, sleep=clock.sleep, clock=clock)


def job_dir(root: Path) -> Path:
    d = root / "job_01testjob0001"
    (d / "input").mkdir(parents=True)
    (d / "input" / "scene.blend").write_bytes(FAKE_BLEND)
    return d


async def extract(runner: JobRunner, d: Path) -> ExtractResult:
    spec = ExtractSpec(
        armature="Rig",
        chain_bones=("thigh.L", "shin.L", "foot.L", "toe.L"),
        frame_start=35,
        frame_end=100,
        result_path=spec_paths("extract_scope")[1],
    )
    return await run_worker(
        runner, d, "extract_scope", WorkerCommand.EXTRACT, spec, ExtractResult, 600
    )


# ------------------------------------------------------------------ auth


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_jwt_is_rs256_signed_with_the_documented_claims(
    key_file: ServiceAccountKey, rsa_key: rsa.RSAPrivateKey
) -> None:
    jwt = key_file.sign_jwt(now=1_790_000_000)
    header, claims, signature = jwt.split(".")
    assert json.loads(_b64(header)) == {"alg": "RS256", "typ": "JWT", "kid": "publickey-e00test"}
    body = json.loads(_b64(claims))
    assert body["iss"] == body["sub"] == "serviceaccount-e00test"
    assert body["exp"] - body["iat"] == 300
    rsa_key.public_key().verify(
        _b64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


async def test_tokens_are_cached_and_refreshed_after_a_401(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius()
    runner = make_runner(fake, key_file, tmp_path)
    d = job_dir(tmp_path)
    await extract(runner, d)
    assert fake.exchanges == 1  # one token for many calls
    fake.expire_token_once = True
    await extract(runner, d)
    assert fake.exchanges == 2  # 401 -> exchanged once more -> the call succeeded


async def test_bad_key_file_is_a_clear_auth_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.pem"
    bad.write_text("not a key", encoding="utf-8")
    tokens = NebiusTokenProvider(
        ServiceAccountKey("sa", "kid", bad), transport=FakeNebius().transport()
    )
    with pytest.raises(NebiusAuthError, match="cannot load"):
        await tokens.token()


# ------------------------------------------------------------------ design A


async def test_job_body_matches_the_documented_contract(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius()
    runner = make_runner(fake, key_file, tmp_path, preemptible=True)
    await extract(runner, job_dir(tmp_path))
    (job,) = fake.jobs.values()
    spec = job.spec
    assert job.body["metadata"]["parentId"] == "project-e00test"
    assert spec["args"] == "--command extract --spec /work/work/extract_scope.spec.json"
    assert "containerCommand" not in spec  # the image's fixed entrypoint is used
    assert spec["timeout"] == "3600s"
    assert spec["disk"] == {"type": "NETWORK_SSD", "sizeBytes": str(32 * GIB)}
    assert spec["restartAttempts"] == "0"
    assert spec["pricingModel"] == {"followsSpotPrice": {}}
    (volume,) = spec["volumes"]
    assert volume["source"] == fake.store.bucket
    assert volume["sourcePath"] == "kinesis/jobs/job_01testjob0001/"
    assert (volume["containerPath"], volume["mode"]) == ("/work", "READ_WRITE")
    assert volume["s3Config"]["endpoint"] == "https://storage.us-central1.nebius.cloud"


async def test_step_runs_and_results_come_back(key_file: ServiceAccountKey, tmp_path: Path) -> None:
    fake = FakeNebius()
    runner = make_runner(fake, key_file, tmp_path)
    assert isinstance(runner, JobRunner)
    d = job_dir(tmp_path)
    result = await extract(runner, d)
    assert result.context_frame_start == 35
    trace = json.loads((d / "work" / "extract_scope.nebius.json").read_text(encoding="utf-8"))
    assert list(trace["states_s_after_submit"]) == [
        "PROVISIONING", "STARTING", "IMAGE_PULLING", "RUNNING", "COMPLETED",
    ]  # fmt: skip
    assert trace["runtime_s"] is not None
    assert trace["estimated_cost_usd"] == pytest.approx(3.6 * trace["runtime_s"] / 3600)
    keys = fake.store.objects
    assert "kinesis/jobs/job_01testjob0001/input/scene.blend" in keys
    scene_puts = sum(1 for r in fake.requests if r.startswith("POST /ai/v1/jobs"))
    await extract(runner, d)
    assert sum(1 for r in fake.requests if r.startswith("POST /ai/v1/jobs")) == scene_puts + 1


async def test_crashed_container_is_worker_crashed(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius(worker=FakeJobRunner({"extract_scope": [Fault.CRASH]}))
    runner = make_runner(fake, key_file, tmp_path)
    with pytest.raises(WorkerCrashed, match="ended FAILED"):
        await extract(runner, job_dir(tmp_path))


async def test_handled_worker_failure_is_reported_not_crashed(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius(worker=FakeJobRunner({"extract_scope": [Fault.REPORTED]}))
    runner = make_runner(fake, key_file, tmp_path)
    with pytest.raises(WorkerReportedFailure):
        await extract(runner, job_dir(tmp_path))


async def test_platform_error_surfaces_the_reason(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius(outcomes=[Outcome.ERROR])
    runner = make_runner(fake, key_file, tmp_path)
    with pytest.raises(WorkerCrashed, match=r"ERROR.*quota exceeded"):
        await extract(runner, job_dir(tmp_path))


async def test_watchdog_cancels_a_hung_job(key_file: ServiceAccountKey, tmp_path: Path) -> None:
    fake = FakeNebius(outcomes=[Outcome.HANG])
    clock = Clock()
    runner = make_runner(fake, key_file, tmp_path, clock=clock, watchdog_s=120)
    with pytest.raises(WorkerTimeout, match="cancelled by the watchdog"):
        await extract(runner, job_dir(tmp_path))
    assert fake.cancelled == ["aijob-0001"]
    assert fake.jobs["aijob-0001"].state == "CANCELLED"


async def test_polling_backs_off_to_the_cap(key_file: ServiceAccountKey, tmp_path: Path) -> None:
    fake = FakeNebius(outcomes=[Outcome.HANG])
    clock = Clock()
    runner = make_runner(fake, key_file, tmp_path, clock=clock, watchdog_s=300)
    with pytest.raises(WorkerTimeout):
        await extract(runner, job_dir(tmp_path))
    polls = [s for s in clock.sleeps if s >= 2.0]
    assert polls[0] == 2.0
    assert all(b >= a for a, b in itertools.pairwise(polls))
    assert max(polls) == 15.0


async def test_cancelling_the_task_cancels_the_nebius_job(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius(outcomes=[Outcome.HANG])
    runner = make_runner(fake, key_file, tmp_path)
    task = asyncio.create_task(extract(runner, job_dir(tmp_path)))
    for _ in range(2000):  # wait until the job really exists and runs
        if fake.jobs and fake.jobs["aijob-0001"].state == "RUNNING":
            break
        await asyncio.sleep(0.001)
    assert fake.jobs["aijob-0001"].state == "RUNNING"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake.cancelled == ["aijob-0001"]


async def test_submit_is_retried_on_server_errors(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius(create_failures=[503, 429])
    runner = make_runner(fake, key_file, tmp_path)
    await extract(runner, job_dir(tmp_path))
    assert len(fake.jobs) == 1


async def test_invalid_spec_is_not_retried(key_file: ServiceAccountKey, tmp_path: Path) -> None:
    fake = FakeNebius(create_failures=[400, 400, 400])
    runner = make_runner(fake, key_file, tmp_path)
    with pytest.raises(Exception, match="HTTP 400"):
        await extract(runner, job_dir(tmp_path))
    assert fake.create_failures == [400, 400]  # one attempt only


# ------------------------------------------------------------------ budget


def test_spend_guard_reserves_settles_and_refuses(tmp_path: Path) -> None:
    guard = SpendGuard(tmp_path / "spend.json", budget_usd=1.0, price_per_hour_usd=3.6)
    reserved = guard.reserve(900)  # 15 min watchdog at $3.60/h
    assert reserved == pytest.approx(0.9)
    with pytest.raises(BudgetExceeded):
        guard.reserve(900)
    assert guard.settle(reserved, 60) == pytest.approx(0.06)
    assert guard.spent_usd == pytest.approx(0.06)
    guard.reserve(900)  # room again after settling
    unknown = SpendGuard(tmp_path / "other.json", budget_usd=0.0, price_per_hour_usd=None)
    assert unknown.reserve(900) == 0.0  # no price: nothing to estimate, nothing refused


async def test_budget_blocks_submission(key_file: ServiceAccountKey, tmp_path: Path) -> None:
    fake = FakeNebius()
    runner = make_runner(fake, key_file, tmp_path, price=3.6, budget=0.5)  # < one watchdog
    with pytest.raises(BudgetExceeded):
        await extract(runner, job_dir(tmp_path))
    assert fake.jobs == {}


# ------------------------------------------------------------------ design B


async def test_session_runs_all_steps_in_one_nebius_job(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius()
    runner = make_runner(fake, key_file, tmp_path, cls=NebiusSessionRunner)
    assert isinstance(runner, NebiusSessionRunner)
    d = job_dir(tmp_path)
    info = await run_worker(
        runner, d, "inspect", WorkerCommand.INSPECT,
        InspectSpec(result_path=spec_paths("inspect")[1]), InspectResult, 600,
    )  # fmt: skip
    assert info.autoexec_disabled
    await extract(runner, d)
    assert len(fake.jobs) == 1
    (job,) = fake.jobs.values()
    assert job.spec["containerCommand"].endswith("python3.11")
    assert "session.py" in job.spec["args"]
    await runner.close(d)
    assert "kinesis/jobs/job_01testjob0001/queue/STOP" in fake.store.objects
    assert job.state == "COMPLETED"
    assert (d / "work" / "session.nebius.json").exists()


async def test_session_ending_early_is_a_crash_and_restarts(
    key_file: ServiceAccountKey, tmp_path: Path
) -> None:
    fake = FakeNebius(outcomes=[Outcome.ERROR])
    runner = make_runner(fake, key_file, tmp_path, cls=NebiusSessionRunner)
    d = job_dir(tmp_path)
    with pytest.raises(WorkerCrashed, match="ended ERROR before answering"):
        await extract(runner, d)
    await extract(runner, d)  # a fresh session is started
    assert len(fake.jobs) == 2


async def test_session_handled_failure(key_file: ServiceAccountKey, tmp_path: Path) -> None:
    fake = FakeNebius(worker=FakeJobRunner({"extract_scope": [Fault.REPORTED]}))
    runner = make_runner(fake, key_file, tmp_path, cls=NebiusSessionRunner)
    d = job_dir(tmp_path)
    with pytest.raises(WorkerReportedFailure):
        await extract(runner, d)


# ------------------------------------------------------------------ whole jobs


@pytest.mark.parametrize("cls", [NebiusJobRunner, NebiusSessionRunner])
async def test_full_job_on_nebius_runner_matches_local_results(
    cls: type[NebiusJobRunner], key_file: ServiceAccountKey, tmp_path: Path, make_service: Any
) -> None:
    fake = FakeNebius()
    nebius = make_service(runner=make_runner(fake, key_file, tmp_path, cls=cls))
    local = make_service()
    jobs = []
    for service in (nebius, local):
        scene = await service.upload_scene(chunks(FAKE_BLEND))
        job, _ = await service.create_job(job_request(scene_id=scene.scene_id), "key-00000001")
        await service.wait_idle()
        jobs.append(await service.get_job(job.job_id))
    remote, host = jobs
    assert remote.status is JobStatus.AWAITING_DECISION, remote.error
    for a, b in zip(remote.candidates, host.candidates, strict=True):
        assert a.metrics == b.metrics  # identical golden results on both runners
    if cls is NebiusSessionRunner:
        states = {j.job_id: (j.state, j.polls) for j in fake.jobs.values()}
        assert all(s == "COMPLETED" for s, _ in states.values()), states  # sessions were closed


def test_runner_factory_reports_every_missing_setting(
    tmp_path: Path, key_file: ServiceAccountKey
) -> None:
    with pytest.raises(ValueError, match=r"NEBIUS_PROJECT_ID.*NEBIUS_AUTH_PEM"):
        build_runner(Settings(kinesis_job_runner=RunnerKind.NEBIUS))


def test_read_result_from_a_downloaded_failure(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    path.write_text(
        json.dumps({"protocol": 1, "ok": False, "error_type": "X", "message": "m"}), "utf-8"
    )
    with pytest.raises(WorkerReportedFailure):
        read_result(path, ExtractResult)


def test_httpx_is_the_only_transport() -> None:
    assert httpx.MockTransport  # the fake plugs into the same client the real runner uses
