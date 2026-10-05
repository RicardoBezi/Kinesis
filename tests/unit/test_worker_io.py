"""Worker spec/result handling, the local runner's argv, and extract -> detection (no Blender)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from kinesis.analysis.extraction import detect_from_extract, motion_from_extract
from kinesis.errors import (
    SelectionInvalid,
    WorkerOutputInvalid,
    WorkerReportedFailure,
    is_retryable,
)
from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.runner import JobRunner, WorkerInvocation
from kinesis.jobs.worker_io import read_result, spec_paths, write_spec
from kinesis.schemas import AnimationSelection, ErrorCode, Severity, SkeletalScope
from kinesis.schemas.worker import ExtractResult, ExtractSpec, InspectResult, WorkerCommand
from kinesis.testing.fake_worker import extract_result
from kinesis.testing.synthetic import foot_slide_v1


@pytest.fixture(scope="module")
def fixture_extract() -> ExtractResult:
    return extract_result(foot_slide_v1(), ("thigh.L", "shin.L", "foot.L", "toe.L"), (35, 100))


def test_spec_paths() -> None:
    assert spec_paths("extract_scope") == (
        "work/extract_scope.spec.json",
        "work/extract_scope.result.json",
    )
    with pytest.raises(ValueError, match="invalid node"):
        spec_paths("../x")


def test_write_spec_round_trips(tmp_path: Path) -> None:
    spec = ExtractSpec(
        armature="Rig",
        chain_bones=("foot.L",),
        frame_start=35,
        frame_end=100,
        result_path="work/extract.result.json",
    )
    path = write_spec(tmp_path, "work/extract.spec.json", spec)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["command"] == "extract"
    assert data["protocol"] == 1
    assert ExtractSpec.model_validate(data) == spec


def test_read_result_missing(tmp_path: Path) -> None:
    with pytest.raises(WorkerOutputInvalid, match="not written"):
        read_result(tmp_path / "nope.json", InspectResult)


@pytest.mark.parametrize(
    "payload",
    ["{not json", json.dumps({"protocol": 1, "ok": True}), json.dumps([1, 2])],
)
def test_read_result_invalid(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "r.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(WorkerOutputInvalid) as err:
        read_result(path, InspectResult)
    assert not is_retryable(err.value)


def test_read_result_reported_failure(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    failure = {"protocol": 1, "ok": False, "error_type": "LookupError", "message": "no Rig"}
    path.write_text(json.dumps(failure), encoding="utf-8")
    with pytest.raises(WorkerReportedFailure) as err:
        read_result(path, InspectResult)
    assert err.value.error_type == "LookupError"
    assert not is_retryable(err.value)


def test_read_result_maps_unsupported_rig(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    failure = {
        "protocol": 1,
        "ok": False,
        "error_type": "UnsupportedRig",
        "message": "armature 'Rig' has world scale (2.0, 2.0, 2.0)\nTraceback ...",
    }
    path.write_text(json.dumps(failure), encoding="utf-8")
    with pytest.raises(SelectionInvalid) as err:
        read_result(path, InspectResult)
    assert err.value.code is ErrorCode.UNSUPPORTED_RIG
    assert "Traceback" not in err.value.message


def test_local_runner_argv_is_fixed(tmp_path: Path) -> None:
    runner = LocalJobRunner(Path("/opt/blender/blender"))
    assert isinstance(runner, JobRunner)
    inv = WorkerInvocation(
        job_dir=tmp_path,
        command=WorkerCommand.EXTRACT,
        spec_relpath="work/extract.spec.json",
        result_relpath="work/extract.result.json",
        timeout_s=10,
    )
    argv = runner.argv(inv)
    assert argv[1:4] == ["--background", "--factory-startup", "-noaudio"]
    assert argv[4] == str((tmp_path / "input" / "scene.blend").resolve())
    assert argv[argv.index("--command") + 1] == "extract"
    assert argv[argv.index("--python-exit-code") + 1] == "3"
    bad = WorkerInvocation(tmp_path, WorkerCommand.EXTRACT, "../spec.json", "r.json", 10)
    with pytest.raises(ValueError, match="unsafe"):
        runner.argv(bad)


async def test_local_runner_health_check_missing_binary(tmp_path: Path) -> None:
    ok, detail = await LocalJobRunner(tmp_path / "blender").health_check()
    assert not ok
    assert "not found" in detail


def test_motion_from_extract(fixture_extract: ExtractResult) -> None:
    motion = motion_from_extract(fixture_extract)
    assert motion.context_range == (35, 100)
    assert motion.scene_range == (1, 120)
    assert motion.chain_world["foot.L"].shape == (66, 4, 4)
    assert motion.heads["hand.R"].shape == (120, 3)
    assert motion.skeleton.names.index("pelvis") < motion.skeleton.names.index("thigh.L")
    sl = motion.context_slice()
    assert np.allclose(motion.heads["foot.L"][sl], motion.chain_world["foot.L"][:, :3, 3])


def test_motion_rejects_inconsistent_samples(fixture_extract: ExtractResult) -> None:
    bones = list(fixture_extract.all_bones)
    bones[0] = bones[0].model_copy(update={"head": bones[0].head[:-1]})
    broken = fixture_extract.model_copy(update={"all_bones": tuple(bones)})
    with pytest.raises(WorkerOutputInvalid, match="samples"):
        motion_from_extract(broken)


def test_detect_from_extract_on_mirror(
    fixture_extract: ExtractResult, selection: AnimationSelection, scope: SkeletalScope
) -> None:
    _, detection = detect_from_extract(selection, scope, fixture_extract)
    worst = detection.report.worst
    assert worst is not None
    assert (worst.start, worst.end) == (39, 91)
    assert 9.5 <= worst.planted_displacement_cm <= 10.5
    assert detection.report.severity is Severity.MAJOR


def test_detect_from_extract_checks_the_window(
    selection: AnimationSelection, scope: SkeletalScope
) -> None:
    short = extract_result(foot_slide_v1(), scope.chain_bones, (40, 100))
    with pytest.raises(WorkerOutputInvalid, match="selection needs"):
        detect_from_extract(selection, scope, short)


def test_foot_tail_used_when_rig_has_no_toe(
    fixture_extract: ExtractResult, selection: AnimationSelection, scope: SkeletalScope
) -> None:
    no_toe = scope.model_copy(update={"chain_bones": ("thigh.L", "shin.L", "foot.L")})
    _, with_toe = detect_from_extract(selection, scope, fixture_extract)
    _, without = detect_from_extract(selection, no_toe, fixture_extract)
    assert without.report == with_toe.report  # fixture's foot tail is the toe head
