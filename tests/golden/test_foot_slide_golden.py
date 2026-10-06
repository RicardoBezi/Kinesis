"""Golden regression tests on the canonical fixture (docs/FIXTURE.md, TESTING.md).

The thresholds live in thresholds.json and the measured snapshot in foot_slide_v1.json
(written by ``test_snapshot_matches_measured_values``). Everything runs on the numpy mirror
of the fixture (``kinesis.testing.synthetic``); ``tests/integration/test_blender_worker.py``
checks the same numbers on the real ``.blend``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kinesis.analysis.detection import detect_foot_slide
from kinesis.analysis.extraction import motion_from_extract
from kinesis.analysis.scope import resolve_temporal
from kinesis.repair.candidates import generate_candidate, repair_input
from kinesis.schemas import (
    ALGORITHM_VERSION,
    AnchorMode,
    AnimationSelection,
    CandidateLabel,
    CandidateParameters,
    Severity,
    compute_candidate_id,
    compute_input_hash,
)
from kinesis.testing.fake_worker import extract_result
from kinesis.testing.fixture_pipeline import (
    CHAIN_BONES,
    CONTEXT,
    LEFT_LEG,
    SELECTION,
    FixtureRun,
    run_fixture,
)
from kinesis.testing.synthetic import foot_slide_v1

pytestmark = pytest.mark.golden

THRESHOLDS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "thresholds.json").read_text(encoding="utf-8")
)
SNAPSHOT = Path(__file__).parent / "foot_slide_v1.json"


def test_thresholds_agree_with_fixture_truth(fixture_truth: dict[str, Any]) -> None:
    assert THRESHOLDS["fixture"] == fixture_truth["fixture"]
    assert (
        THRESHOLDS["detection"]["planted_displacement_cm"]
        == fixture_truth["expected"]["planted_displacement_cm"]
    )
    assert THRESHOLDS["detection"]["severity"] == fixture_truth["expected"]["severity"]


def test_threshold_ordering_is_coherent() -> None:
    c = THRESHOLDS["candidates"]
    assert c["A"]["slip_reduction_pct_min"] >= c["best"]["slip_reduction_pct_min"] >= 80.0
    assert 0 < c["B"]["slip_reduction_pct_min"] <= c["A"]["slip_reduction_pct_min"]


# ------------------------------------------------------------------ Phase 1: detection


def test_detection_measures_injected_slide(
    fixture_truth: dict[str, Any], selection: AnimationSelection, scene_range: tuple[int, int]
) -> None:
    """Phase 1 exit criterion: about 10 cm of planted displacement on frames 40-90."""
    scene = foot_slide_v1()
    start, end = resolve_temporal(selection.temporal, scene_range)
    window = slice(start - scene.frame_start, end - scene.frame_start + 1)
    detection = detect_foot_slide(
        scene.head("foot.L")[window],
        scene.head("toe.L")[window],
        frame_start=start,
        fps=scene.fps,
        selection=(selection.temporal.frame_start, selection.temporal.frame_end),
        bone="foot.L",
    )
    worst = detection.report.worst
    assert worst is not None
    tol = THRESHOLDS["detection"]["interval_tolerance_frames"]
    expected_start, expected_end = fixture_truth["planted_interval"]
    assert abs(worst.start - expected_start) <= tol
    assert abs(worst.end - expected_end) <= tol
    band = THRESHOLDS["detection"]["planted_displacement_cm"]
    assert band["min"] <= worst.planted_displacement_cm <= band["max"]
    assert detection.report.severity is Severity(THRESHOLDS["detection"]["severity"])


def test_clean_fixture_motion_is_not_flagged(selection: AnimationSelection) -> None:
    """Without the injected defect the same selection is planted but not sliding."""
    scene = foot_slide_v1(defect=False)
    window = slice(35 - scene.frame_start, 100 - scene.frame_start + 1)
    detection = detect_foot_slide(
        scene.head("foot.L")[window],
        scene.head("toe.L")[window],
        frame_start=35,
        fps=scene.fps,
        selection=(selection.temporal.frame_start, selection.temporal.frame_end),
    )
    assert detection.report.severity is Severity.NONE
    assert detection.report.intervals


# ------------------------------------------------------------------ Phase 2: repair


@pytest.fixture(scope="module")
def run() -> FixtureRun:
    return run_fixture()


@pytest.mark.parametrize("label", ["A", "B"])
def test_candidate_meets_thresholds(run: FixtureRun, label: str) -> None:
    m = run.candidates[CandidateLabel(label)].metrics
    t = THRESHOLDS["all_candidates"]
    assert m.slip_reduction_pct >= THRESHOLDS["candidates"][label]["slip_reduction_pct_min"]
    assert m.collateral_max_cm <= t["collateral_max_cm_max"]
    assert m.outside_window_max_cm <= t["outside_window_max_cm_max"]
    assert m.joint_limit_violations <= t["joint_limit_violations_max"]
    assert m.penetration_max_cm <= t["penetration_max_cm_max"]
    assert m.jerk_rms_ratio <= t["jerk_rms_ratio_max"]
    assert m.ik_unreachable_frames <= t["ik_unreachable_frames_max"]
    assert not m.gated
    assert m.root_deviation_cm == 0.0


def test_best_candidate_meets_spec_target(run: FixtureRun) -> None:
    best = max(c.metrics.slip_reduction_pct for c in run.candidates.values())
    assert best >= THRESHOLDS["candidates"]["best"]["slip_reduction_pct_min"]


def _measured(run: FixtureRun) -> dict[str, Any]:
    worst = run.detection.report.worst
    assert worst is not None
    return {
        "fixture": "foot_slide_v1",
        "algorithm_version": ALGORITHM_VERSION,
        "detected_interval": list(run.interval),
        "planted_displacement_cm": round(worst.planted_displacement_cm, 6),
        "candidates": {
            label.value: {
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in c.metrics.model_dump(mode="json").items()
            }
            for label, c in sorted(run.candidates.items())
        },
    }


def test_snapshot_matches_measured_values(run: FixtureRun) -> None:
    """Regenerate with ``KINESIS_UPDATE_GOLDEN=1`` and explain the change in the commit."""
    measured = _measured(run)
    if os.environ.get("KINESIS_UPDATE_GOLDEN") == "1":
        SNAPSHOT.write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")
    stored = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert stored["algorithm_version"] == measured["algorithm_version"]
    assert stored["detected_interval"] == measured["detected_interval"]
    for label, values in stored["candidates"].items():
        for key, value in values.items():
            got = measured["candidates"][label][key]
            if isinstance(value, float):
                assert got == pytest.approx(value, abs=1e-5), (label, key)
            else:
                assert got == value, (label, key)


# ------------------------------------------------------------------ preservation invariants


def test_right_hand_unchanged_by_left_foot_repair(run: FixtureRun) -> None:
    for c in run.candidates.values():
        for bone in ("hand.R", "forearm.R", "foot.R", "hand.L", "pelvis", "head"):
            assert np.abs(c.scene.head(bone) - run.scene.head(bone)).max() <= 1e-6
            assert np.abs(c.scene.tail(bone) - run.scene.tail(bone)).max() <= 1e-6


def test_input_arrays_not_mutated() -> None:
    scene = foot_slide_v1()
    before = {n: m.copy() for n, m in scene.world.items()}
    basis = {n: m.copy() for n, m in scene.basis.items()}
    run_fixture(scene=scene)
    for name, m in scene.world.items():
        assert np.array_equal(m, before[name]), name
        assert np.array_equal(scene.basis[name], basis[name]), name


def test_candidate_id_deterministic(run: FixtureRun) -> None:
    scope = {"selection": list(SELECTION), "interval": list(run.interval), "chain": CHAIN_BONES}

    def ids(r: FixtureRun) -> dict[CandidateLabel, str]:
        features = r.detection.features.model_dump_json().encode()
        return {
            label: compute_candidate_id(compute_input_hash(scope, features), c.parameters)
            for label, c in r.candidates.items()
        }

    first = ids(run)
    assert ids(run_fixture()) == first
    assert first[CandidateLabel.A] != first[CandidateLabel.B]


def test_repair_idempotent(run: FixtureRun) -> None:
    """Re-running the full-strength lock on its own output, over the same interval, changes
    nothing where the lock is at full weight. (Blend-ramp frames are not idempotent by
    construction: a second pass moves them by another ``w (1 - w)`` of the remaining offset.)
    """
    a = run.candidates[CandidateLabel.A]
    motion = motion_from_extract(extract_result(a.scene, CHAIN_BONES, CONTEXT))
    inp = repair_input(motion, LEFT_LEG, run.interval, run.detection.floor_height)
    second = generate_candidate(inp, a.parameters)
    lo, hi = run.interval
    for bone, (frames, quats) in second.keys.items():
        for f, q in zip(frames, quats, strict=True):
            if lo <= f <= hi:
                prev = a.scene.local_quat[bone][f - a.scene.frame_start]
                assert abs(float(np.dot(q, prev))) == pytest.approx(1.0, abs=1e-9), (bone, f)


params_strategy = st.builds(
    CandidateParameters,
    lock_strength=st.floats(0.0, 1.0),
    anchor_mode=st.sampled_from(list(AnchorMode)),
    blend_frames=st.integers(0, 24),
    tolerance_cm=st.floats(0.0, 5.0),
    lock_yaw=st.booleans(),
    smoothing_window=st.sampled_from([0, 3, 5, 7, 9]),
    height_clamp=st.booleans(),
)


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(params=params_strategy)
def test_no_keys_outside_blend_window(params: CandidateParameters) -> None:
    result = run_fixture({CandidateLabel.A: params})
    c = result.candidates[CandidateLabel.A]
    a, b = result.interval
    k = params.blend_frames
    assert set(c.result.keys) <= {"thigh.L", "shin.L", "foot.L"}
    for frames, _ in c.result.keys.values():
        assert all(a - k <= f <= b + k for f in frames)
        assert all(CONTEXT[0] <= f <= CONTEXT[1] for f in frames)
    assert c.metrics.outside_window_max_cm == 0.0
    assert c.metrics.collateral_max_cm == 0.0
    assert c.metrics.root_deviation_cm == 0.0
