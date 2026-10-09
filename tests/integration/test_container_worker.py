"""Phase 6 parity: the containerized worker gives the same golden results as host Blender.

Needs docker and the image from ``uv run task worker-image``; run with
``uv run task test-container``. The container has no network and a read-only root
filesystem, so this also proves the worker needs nothing beyond its job directory.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from kinesis.jobs.container import DEFAULT_IMAGE, ContainerJobRunner
from kinesis.jobs.service import JobService, ServiceConfig
from kinesis.jobs.worker_io import run_worker, spec_paths
from kinesis.providers.null import NullProvider
from kinesis.schemas import CandidateLabel, DecisionChoice, DecisionRequest, JobStatus
from kinesis.schemas.worker import InspectResult, InspectSpec, WorkerCommand
from kinesis.storage.sqlite import Database, SqliteArtifactStore, SqliteJobStore

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "blender" / "fixtures" / "foot_slide_v1.blend"
GOLDEN: dict[str, Any] = json.loads(
    (REPO / "tests" / "golden" / "foot_slide_v1.json").read_text("utf-8")
)


def _image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(  # noqa: S603 - fixed argv
        ["docker", "image", "inspect", DEFAULT_IMAGE],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


pytestmark = [
    pytest.mark.container,
    pytest.mark.skipif(not _image_available(), reason=f"docker image {DEFAULT_IMAGE} not built"),
]


def _dump_logs(root: Path) -> None:
    """Worker logs (docker's own errors included) are the only evidence when a run fails."""
    for log in sorted(root.rglob("*.log")):
        print(f"--- {log.relative_to(root)}")
        print(log.read_text(errors="replace")[-3000:])


async def test_container_job_matches_golden_results(tmp_path: Path) -> None:
    from conftest import chunks, job_request

    db = Database(tmp_path / "k.db")
    service = JobService(
        SqliteJobStore(db),
        SqliteArtifactStore(db, tmp_path / "jobs"),
        ContainerJobRunner(),
        NullProvider(),
        ServiceConfig(data_dir=tmp_path),
    )
    assert (await service.runner.health_check())[0]
    # A direct inspect first: if `docker run` cannot start, its reason is in the error message.
    probe = tmp_path / "probe"
    (probe / "input").mkdir(parents=True)
    shutil.copyfile(FIXTURE, probe / "input" / "scene.blend")
    info = await run_worker(
        service.runner, probe, "inspect", WorkerCommand.INSPECT,
        InspectSpec(result_path=spec_paths("inspect")[1]), InspectResult, 300,
    )  # fmt: skip
    assert info.autoexec_disabled
    started = time.monotonic()
    try:
        scene = await service.upload_scene(chunks(FIXTURE.read_bytes(), 1 << 20))
    except Exception:
        _dump_logs(tmp_path)
        raise
    assert scene.blender_version.startswith("4.5")
    job, _ = await service.create_job(job_request(scene_id=scene.scene_id), "container-0001")
    await service.wait_idle()
    job = await service.get_job(job.job_id)
    elapsed = time.monotonic() - started
    if job.status is not JobStatus.AWAITING_DECISION:
        _dump_logs(tmp_path)
    assert job.status is JobStatus.AWAITING_DECISION, job.error

    worst = job.defect.worst if job.defect else None
    assert worst is not None
    assert [worst.start, worst.end] == GOLDEN["detected_interval"]
    assert worst.planted_displacement_cm == pytest.approx(
        GOLDEN["planted_displacement_cm"], abs=1e-3
    )
    for candidate in job.candidates:
        golden = GOLDEN["candidates"][candidate.label.value]
        m = candidate.metrics
        assert m is not None
        assert m.slip_reduction_pct == pytest.approx(golden["slip_reduction_pct"], abs=0.01)
        assert m.jerk_rms_ratio == pytest.approx(golden["jerk_rms_ratio"], abs=1e-3)
        assert m.collateral_max_cm <= 0.01
        assert m.outside_window_max_cm <= 0.001
        assert {a.kind.value for a in candidate.artifacts} >= {"PREVIEW_FRAMES", "CANDIDATE_BLEND"}
    assert job.evaluation is not None
    assert job.evaluation.recommended is CandidateLabel.B

    await service.decide(job.job_id, DecisionRequest(choice=DecisionChoice.B))
    await service.wait_idle()
    done = await service.get_job(job.job_id)
    assert done.status is JobStatus.COMPLETED, done.error
    assert done.output is not None
    output = await service.artifacts.resolve(done.output.artifact_id)
    assert output.read_bytes().startswith(b"BLENDER")
    print(f"\ncontainer job: upload -> review in {elapsed:.1f} s")
