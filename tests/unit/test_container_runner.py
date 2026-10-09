"""ContainerJobRunner argv: isolation flags and the fixed worker contract (no docker needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinesis.api.deps import build_runner
from kinesis.jobs.container import ContainerJobRunner
from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.runner import JobRunner, WorkerInvocation
from kinesis.schemas.worker import WorkerCommand
from kinesis.settings import RunnerKind, Settings


def invocation(tmp_path: Path) -> WorkerInvocation:
    return WorkerInvocation(
        job_dir=tmp_path,
        command=WorkerCommand.APPLY_RENDER,
        spec_relpath="work/apply_a.spec.json",
        result_relpath="work/apply_a.result.json",
        timeout_s=300,
    )


def test_container_argv_is_isolated_and_fixed(tmp_path: Path) -> None:
    runner = ContainerJobRunner("kinesis-worker:test")
    assert isinstance(runner, JobRunner)
    argv, on_abort = runner.launch(invocation(tmp_path))
    assert on_abort is not None  # cancel/timeout force-removes the container
    assert argv[:3] == ["docker", "run", "--rm"]
    for flag in (["--network", "none"], ["--read-only"], ["--security-opt", "no-new-privileges"]):
        i = argv.index(flag[0])
        assert argv[i : i + len(flag)] == flag
    assert f"{tmp_path.resolve()}:/work" in argv  # the job directory is the only mount
    assert argv[argv.index("kinesis-worker:test") + 1 :] == [
        "--command",
        "apply_render",
        "--spec",
        "/work/work/apply_a.spec.json",
    ]
    name = argv[argv.index("--name") + 1]
    assert name.startswith("kinesis-")
    again = runner.launch(invocation(tmp_path))[0]
    assert again[again.index("--name") + 1] != name  # every run gets its own container


def test_cpu_cap_never_exceeds_the_host(tmp_path: Path) -> None:
    import os

    argv, _ = ContainerJobRunner().launch(invocation(tmp_path))
    cpus = float(argv[argv.index("--cpus") + 1])
    assert 0 < cpus <= min(4, os.cpu_count() or 1)
    explicit, _ = ContainerJobRunner(cpus=1.5).launch(invocation(tmp_path))
    assert explicit[explicit.index("--cpus") + 1] == "1.5"


def test_container_argv_rejects_escaping_specs(tmp_path: Path) -> None:
    bad = WorkerInvocation(tmp_path, WorkerCommand.EXTRACT, "../x.spec.json", "r.json", 10)
    with pytest.raises(ValueError, match="unsafe"):
        ContainerJobRunner().launch(bad)


def test_runner_selection_from_settings() -> None:
    container = build_runner(Settings(kinesis_job_runner=RunnerKind.CONTAINER))
    assert isinstance(container, ContainerJobRunner)
    assert isinstance(build_runner(Settings(kinesis_job_runner=RunnerKind.LOCAL)), LocalJobRunner)
    with pytest.raises(ValueError, match="NEBIUS_PROJECT_ID"):  # needs its configuration
        build_runner(Settings(kinesis_job_runner=RunnerKind.NEBIUS))
