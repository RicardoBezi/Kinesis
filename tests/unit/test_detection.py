"""Foot-slide detection (ALGORITHMS §2) on hand-built micro-trajectories."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from kinesis.analysis.detection import (
    ContactPlane,
    close_gaps,
    detect_foot_slide,
    horizontal_speed,
    project_to_plane,
    remove_short_runs,
    runs,
    severity_for,
)
from kinesis.schemas import DetectionConfig, Severity

FPS = 24.0
TOE_OFFSET = np.array([0.0, -0.12, -0.06])


def walk(n: int, planted: slice, slide: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Ankle at z=0.08 while planted (sliding ``slide`` m along X), lifted to 0.3 elsewhere.

    Swing frames move 0.1 m/frame, so the central-difference speed of the first and last
    planted frames (half a swing step, 1.2 m/s) exceeds V: detected runs are one frame
    shorter than ``planted`` at each end.
    """
    foot = np.zeros((n, 3))
    foot[:, 2] = 0.3
    foot[:, 1] = -np.arange(n) * 0.1  # swing frames move fast
    idx = np.arange(n)[planted]
    foot[idx, 1] = foot[idx[0], 1]
    foot[idx, 2] = 0.08
    foot[idx, 0] = np.linspace(0, slide, len(idx))
    return foot, foot + TOE_OFFSET


def mask(bits: str) -> np.ndarray:
    return np.array([c == "1" for c in bits])


@pytest.mark.parametrize(
    ("bits", "expected"),
    [("0110111", [(1, 2), (4, 6)]), ("", []), ("1", [(0, 0)]), ("000", [])],
)
def test_runs(bits: str, expected: list[tuple[int, int]]) -> None:
    assert runs(mask(bits)) == expected


@pytest.mark.parametrize(
    ("bits", "gap", "expected"),
    [
        ("1101", 2, "1111"),
        ("11001", 2, "11111"),
        ("110001", 2, "110001"),  # gap of 3 stays open
        ("0011", 2, "0011"),  # leading false run is not bounded on both sides
        ("1100", 2, "1100"),
        ("101", 0, "101"),
    ],
)
def test_close_gaps(bits: str, gap: int, expected: str) -> None:
    original = mask(bits)
    out = close_gaps(original, gap)
    assert "".join("1" if b else "0" for b in out) == expected
    assert np.array_equal(original, mask(bits))  # pure


def test_remove_short_runs() -> None:
    out = remove_short_runs(mask("1110111111"), 6)
    assert "".join("1" if b else "0" for b in out) == "0000111111"


def test_speed_central_and_one_sided() -> None:
    u = np.array([[0.0, 0, 0], [1, 0, 0], [3, 0, 0], [6, 0, 0]])
    s = horizontal_speed(u, 2.0)
    assert s.tolist() == [2.0, 3.0, 5.0, 6.0]


def test_projection_on_tilted_plane() -> None:
    plane = ContactPlane(origin=(0, 0, 1), normal=(0, 0, 2))  # normal is normalized
    assert np.allclose(project_to_plane(np.array([[1.0, 2, 5]]), plane), [[1, 2, 1]])


@pytest.mark.parametrize(
    ("cm", "severity"),
    [(0.0, Severity.NONE), (0.49, Severity.NONE), (0.5, Severity.MINOR), (2.0, Severity.MAJOR)],
)
def test_severity_bands(cm: float, severity: Severity) -> None:
    assert severity_for(cm, DetectionConfig()) is severity


def test_detects_slide_and_reports_metrics() -> None:
    foot, toe = walk(60, slice(10, 40), slide=0.05)
    d = detect_foot_slide(foot, toe, frame_start=100, fps=FPS, selection=(100, 159), bone="foot.L")
    (iv,) = d.report.intervals
    assert (iv.start, iv.end) == (111, 138)
    assert iv.planted_displacement_cm == pytest.approx(5.0 * 27 / 29)
    assert iv.path_slip_cm == pytest.approx(5.0 * 27 / 29)
    assert iv.max_frame_slip_cm == pytest.approx(5.0 / 29)
    assert iv.mean_slip_velocity_cm_s == pytest.approx(5.0 / 29 * FPS)
    assert iv.velocity_variance == pytest.approx(0.0, abs=1e-20)
    assert iv.contact_consistency == 1.0
    assert iv.anchor == pytest.approx((foot[11, 0], foot[11, 1], 0.0))
    assert d.report.severity is Severity.MAJOR
    assert "4.7 cm" in d.report.summary
    assert d.floor_height == pytest.approx(0.02)
    assert d.features.planted_mask[10:40] == (False, *(True,) * 28, False)


def test_clean_contact_has_no_defect() -> None:
    foot, toe = walk(60, slice(10, 40))
    d = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(1, 60))
    assert d.report.severity is Severity.NONE
    assert len(d.report.intervals) == 1
    assert d.report.intervals[0].planted_displacement_cm == 0.0


def test_never_planted() -> None:
    n = 30
    foot = np.column_stack([np.zeros(n), -np.arange(n) * 0.1, 0.08 + 0.1 * np.sin(np.arange(n))])
    d = detect_foot_slide(foot, foot + TOE_OFFSET, frame_start=1, fps=FPS, selection=(1, n))
    assert d.report.intervals == ()
    assert d.report.worst is None
    assert d.report.severity is Severity.NONE
    assert "no planted contact" in d.report.summary


def test_all_planted() -> None:
    foot, toe = walk(30, slice(0, 30), slide=0.01)
    d = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(1, 30))
    (iv,) = d.report.intervals
    assert (iv.start, iv.end) == (1, 30)
    assert d.report.severity is Severity.MINOR


def test_short_gap_is_closed_and_short_run_removed() -> None:
    foot, toe = walk(60, slice(10, 40))
    foot[20:22, 2] = toe[20:22, 2] = 0.5  # 2-frame blip: closed
    foot[50:53] = foot[10]  # 3-frame touch: too short
    toe[50:53] = toe[10]
    d = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(1, 60))
    assert [(i.start, i.end) for i in d.report.intervals] == [(12, 39)]


def test_only_intervals_touching_the_selection_are_reported() -> None:
    foot, toe = walk(80, slice(5, 20))
    foot[50:70] = foot[5]
    toe[50:70] = toe[5]
    d = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(55, 60))
    assert [(i.start, i.end) for i in d.report.intervals] == [(52, 69)]


def test_worst_interval_wins() -> None:
    foot, toe = walk(80, slice(5, 25), slide=0.01)
    foot[50:70, :2] = [0.0, foot[50, 1]]
    foot[50:70, 0] = np.linspace(0, 0.04, 20)
    foot[50:70, 2] = 0.08
    toe = foot + TOE_OFFSET
    d = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(1, 80))
    assert d.report.worst is not None
    assert d.report.worst.start == 52
    assert d.report.worst.planted_displacement_cm == pytest.approx(4.0 * 17 / 19)


def test_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="shape"):
        detect_foot_slide(
            np.zeros((5, 2)), np.zeros((5, 2)), frame_start=1, fps=24, selection=(1, 5)
        )


@given(st.floats(0.0, 0.2), st.integers(10, 40))
def test_detection_is_pure_and_deterministic(slide: float, length: int) -> None:
    foot, toe = walk(60, slice(10, 10 + length), slide=slide)
    foot_copy, toe_copy = foot.copy(), toe.copy()
    a = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(1, 60))
    b = detect_foot_slide(foot, toe, frame_start=1, fps=FPS, selection=(1, 60))
    assert a.report == b.report
    assert np.array_equal(foot, foot_copy)
    assert np.array_equal(toe, toe_copy)
    if length - 2 >= DetectionConfig().min_run_frames:
        worst = a.report.worst
        assert worst is not None
        expected = 100 * slide * (length - 3) / (length - 1)
        assert worst.planted_displacement_cm == pytest.approx(expected, abs=1e-9)
    else:
        assert a.report.intervals == ()
