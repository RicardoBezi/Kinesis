"""Live Nebius Serverless Jobs test. It NEVER runs by default and costs money.

Gates: the ``live_nebius_jobs`` marker, ``KINESIS_LIVE_NEBIUS_JOBS=1``, and a complete
``KINESIS_JOB_RUNNER=nebius`` configuration (see docs/PHASE6.md). Run manually with
``uv run task live-nebius-jobs-test``. One inspect step on the fixture: one small CPU job,
cancelled by the watchdog if it runs long, and refused by the spend guard over budget.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest

from kinesis.api.deps import build_nebius_runner
from kinesis.jobs.worker_io import run_worker, spec_paths
from kinesis.schemas.worker import InspectResult, InspectSpec, WorkerCommand
from kinesis.settings import RunnerKind, Settings

pytestmark = pytest.mark.live_nebius_jobs

FIXTURE = Path(__file__).resolve().parents[2] / "blender" / "fixtures" / "foot_slide_v1.blend"


async def test_inspect_step_runs_on_nebius(tmp_path: Path) -> None:
    settings = Settings().model_copy(update={"kinesis_job_runner": RunnerKind.NEBIUS})
    try:
        runner = build_nebius_runner(settings)
    except ValueError as exc:
        pytest.skip(f"Nebius runner not configured: {exc}")
    job_dir = tmp_path / "job_live000001"
    (job_dir / "input").mkdir(parents=True)
    shutil.copyfile(FIXTURE, job_dir / "input" / "scene.blend")
    started = time.monotonic()
    info = await run_worker(
        runner,
        job_dir,
        "inspect",
        WorkerCommand.INSPECT,
        InspectSpec(result_path=spec_paths("inspect")[1]),
        InspectResult,
        settings.nebius_watchdog_s,
    )
    elapsed = time.monotonic() - started
    assert info.blender_version.startswith("4.5")
    trace = (job_dir / "work" / "inspect.nebius.json").read_text(encoding="utf-8")
    print(f"\nMEASURED on Nebius: inspect in {elapsed:.1f} s\n{trace}")
