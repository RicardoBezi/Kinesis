"""Headless Blender integration tests. They run with ``uv run task test-blender``, or in the
CI ``blender.yml`` workflow.

They need a Blender 4.5 executable at KINESIS_BLENDER_BIN and the committed fixture
``blender/fixtures/foot_slide_v1.blend`` (rebuild with ``uv run task fixture``).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from kinesis.analysis.extraction import detect_from_extract, motion_from_extract
from kinesis.errors import WorkerReportedFailure
from kinesis.jobs.local import LocalJobRunner
from kinesis.jobs.worker_io import run_worker, spec_paths
from kinesis.schemas import AnimationSelection, Severity, SkeletalScope
from kinesis.schemas.worker import (
    ExtractResult,
    ExtractSpec,
    InspectResult,
    InspectSpec,
    WorkerCommand,
)
from kinesis.testing.synthetic import RIG_BONES, foot_slide_v1

pytestmark = [
    pytest.mark.blender,
    pytest.mark.skipif(not os.environ.get("KINESIS_BLENDER_BIN"), reason="KINESIS_BLENDER_BIN"),
]

REPO = Path(__file__).resolve().parents[2]
FIXTURE_BLEND = REPO / "blender" / "fixtures" / "foot_slide_v1.blend"
BUILDER = REPO / "blender" / "fixtures" / "build_fixture.py"
TIMEOUT_S = 300
MIRROR_TOLERANCE_M = 1e-4  # tests/golden/thresholds.json: blender_vs_numpy_mirror_max_mm


def _blender() -> Path:
    return Path(os.environ["KINESIS_BLENDER_BIN"])


def _job_dir(root: Path, blend: Path = FIXTURE_BLEND) -> Path:
    (root / "input").mkdir(parents=True)
    shutil.copyfile(blend, root / "input" / "scene.blend")
    return root


def _inspect(job_dir: Path) -> InspectResult:
    spec = InspectSpec(result_path=spec_paths("inspect")[1])
    runner = LocalJobRunner(_blender())
    return asyncio.run(
        run_worker(
            runner, job_dir, "inspect", WorkerCommand.INSPECT, spec, InspectResult, TIMEOUT_S
        )
    )


def _extract(job_dir: Path, scope: SkeletalScope, window: tuple[int, int]) -> ExtractResult:
    spec = ExtractSpec(
        armature=scope.armature,
        chain_bones=scope.chain_bones,
        frame_start=window[0],
        frame_end=window[1],
        result_path=spec_paths("extract_scope")[1],
    )
    runner = LocalJobRunner(_blender())
    return asyncio.run(
        run_worker(
            runner, job_dir, "extract_scope", WorkerCommand.EXTRACT, spec, ExtractResult, TIMEOUT_S
        )
    )


@pytest.fixture(scope="module")
def module_scope() -> SkeletalScope:
    return SkeletalScope(
        armature="Rig",
        target_bones=("foot.L",),
        chain_bones=("thigh.L", "shin.L", "foot.L", "toe.L"),
        keyable_bones=("thigh.L", "shin.L", "foot.L"),
        context_bones=("pelvis", "root"),
    )


@pytest.fixture(scope="module")
def inspected(tmp_path_factory: pytest.TempPathFactory) -> InspectResult:
    return _inspect(_job_dir(tmp_path_factory.mktemp("inspect")))


@pytest.fixture(scope="module")
def extracted(
    tmp_path_factory: pytest.TempPathFactory, module_scope: SkeletalScope
) -> ExtractResult:
    return _extract(_job_dir(tmp_path_factory.mktemp("extract")), module_scope, (35, 100))


# ------------------------------------------------------------------ Phase 1


def test_scene_loads_and_inspect_reports_rig(
    inspected: InspectResult, fixture_truth: dict[str, Any]
) -> None:
    assert inspected.blender_version.startswith("4.5")
    assert (inspected.frame_start, inspected.frame_end) == tuple(fixture_truth["frame_range"])
    assert inspected.fps == fixture_truth["fps"]
    (rig,) = inspected.armatures
    assert rig.name == fixture_truth["armature"]
    assert {(b.name, b.parent) for b in rig.bones} == {(n, p) for n, p, *_ in RIG_BONES}
    assert all(b.deform for b in rig.bones)


def test_autoexec_scripts_disabled(inspected: InspectResult) -> None:
    assert inspected.autoexec_disabled


def test_extract_matches_numpy_mirror_within_0_1mm(extracted: ExtractResult) -> None:
    mirror = foot_slide_v1()
    motion = motion_from_extract(extracted)
    worst = 0.0
    for name in mirror.skeleton.names:
        worst = max(worst, float(np.abs(motion.heads[name] - mirror.head(name)).max()))
        worst = max(worst, float(np.abs(motion.tails[name] - mirror.tail(name)).max()))
        assert np.allclose(motion.skeleton.rest[name], mirror.skeleton.rest[name], atol=1e-6)
    sl = slice(35 - mirror.frame_start, 100 - mirror.frame_start + 1)
    for name, world in motion.chain_world.items():
        worst = max(worst, float(np.abs(world - mirror.world[name][sl]).max()))
    assert worst < MIRROR_TOLERANCE_M, f"max deviation {worst:.3e} m"


def test_phase1_exit_criterion_on_real_fixture(
    extracted: ExtractResult,
    selection: AnimationSelection,
    module_scope: SkeletalScope,
    fixture_truth: dict[str, Any],
) -> None:
    """About 10 cm of planted displacement on frames 40-90, measured on the real .blend."""
    _, detection = detect_from_extract(selection, module_scope, extracted)
    worst = detection.report.worst
    assert worst is not None
    tol = fixture_truth["expected"]["detected_interval_tolerance_frames"]
    start, end = fixture_truth["planted_interval"]
    assert abs(worst.start - start) <= tol
    assert abs(worst.end - end) <= tol
    band = fixture_truth["expected"]["planted_displacement_cm"]
    assert band["min"] <= worst.planted_displacement_cm <= band["max"]
    assert detection.report.severity is Severity(fixture_truth["expected"]["severity"])


def test_extract_reports_unknown_armature(tmp_path: Path, module_scope: SkeletalScope) -> None:
    job_dir = _job_dir(tmp_path)
    with pytest.raises(WorkerReportedFailure, match="armature 'Nope' not found"):
        _extract(job_dir, module_scope.model_copy(update={"armature": "Nope"}), (35, 100))


def test_committed_fixture_matches_builder(
    tmp_path: Path, extracted: ExtractResult, module_scope: SkeletalScope
) -> None:
    """Rebuilding the fixture yields the same keys: the .blend has not drifted from the mirror."""
    rebuilt = tmp_path / "rebuilt.blend"
    proc = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [
            str(_blender()),
            *("--background", "--factory-startup", "-noaudio", "--python-exit-code", "3"),
            *("--python", str(BUILDER), "--", "--out", str(rebuilt)),
        ],
        capture_output=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout.decode(errors="replace")[-2000:]
    again = _extract(_job_dir(tmp_path / "job", rebuilt), module_scope, (35, 100))
    assert again.original_action_hash == extracted.original_action_hash


# ------------------------------------------------------------------ Phase 2 placeholders

PHASE2 = [
    "candidate_action_created_on_nla_track",
    "original_action_fcurves_hash_unchanged",
    "render_produces_expected_frame_count",
    "metrics_produced_and_defect_reduced",
    "export_writes_output_blend_with_selected_track",
]


@pytest.mark.parametrize("check", PHASE2)
def test_blender_worker_phase2(check: str) -> None:
    pytest.skip(f"Phase 2: {check}")
