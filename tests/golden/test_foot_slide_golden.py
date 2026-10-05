"""Golden regression tests on the canonical fixture (docs/FIXTURE.md, TESTING.md).

The thresholds live in thresholds.json and the measured snapshots in foot_slide_v1.json
(written in Phase 2). Detection runs on the numpy mirror of the fixture
(``kinesis.testing.synthetic``); ``tests/integration/test_blender_worker.py`` checks that the
mirror matches the real ``.blend``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kinesis.analysis.detection import detect_foot_slide
from kinesis.analysis.scope import resolve_temporal
from kinesis.schemas import AnimationSelection, Severity
from kinesis.testing.synthetic import foot_slide_v1

pytestmark = pytest.mark.golden

THRESHOLDS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "thresholds.json").read_text(encoding="utf-8")
)


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


@pytest.mark.skip(reason="Phase 2: candidate A/B repair on the numpy mirror")
@pytest.mark.parametrize("label", ["A", "B"])
def test_candidate_meets_thresholds(label: str) -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 2: preservation invariants (hypothesis) on the numpy mirror")
@pytest.mark.parametrize(
    "invariant",
    [
        "right_hand_unchanged_by_left_foot_repair",
        "input_arrays_not_mutated",
        "no_keys_outside_blend_window",
        "candidate_id_deterministic",
        "repair_idempotent",
    ],
)
def test_preservation_invariants(invariant: str) -> None:
    raise NotImplementedError
