"""Objective metrics and ranking (ALGORITHMS §4, §4.1) on hand-built samples."""

from __future__ import annotations

import numpy as np
import pytest

from kinesis.evaluation.metrics import (
    MetricContext,
    Samples,
    compute_metrics,
    jerk_rms,
    knee_flexion_deg,
    objective_score,
    rank_candidates,
)
from kinesis.schemas import CandidateLabel, CandidateMetrics

T = 40
CTX = MetricContext(
    fps=24.0,
    thigh="thigh",
    shin="shin",
    foot="foot",
    toe=None,
    chain_bones=frozenset({"thigh", "shin", "foot"}),
    root_bones=("pelvis",),
    interval=(11, 30),
    blend_frames=2,
    context_range=(1, 40),
    floor_height=0.02,
)


def leg(slide: float = 0.0, knee_bend: float = 0.05) -> Samples:
    """A static leg; the foot slides ``slide`` m along X over the interval (frames 11-30)."""
    x = np.zeros(T)
    x[10:30] = np.linspace(0, slide, 20)
    x[30:] = slide
    foot = np.column_stack([x, np.zeros(T), np.full(T, 0.08)])
    hip = np.tile([0.0, 0.0, 0.9], (T, 1))
    knee = np.tile([0.0, -knee_bend, 0.5], (T, 1))
    knee[:, 0] = x / 2
    heads = {"pelvis": hip.copy(), "thigh": hip, "shin": knee, "foot": foot, "hand": hip + 0.3}
    tails = {
        "pelvis": hip + np.array([0, 0, 0.1]),
        "thigh": knee,
        "shin": foot,
        "foot": foot + np.array([0, -0.12, -0.06]),
        "hand": hip + 0.4,
    }
    return Samples(frame_start=1, heads=heads, tails=tails)


def locked(original: Samples, keep: float) -> Samples:
    """``original`` with the foot's slide inside the interval scaled by ``keep``.

    Frames outside the interval are untouched, like a repair with no blend frames.
    """
    heads = {k: v.copy() for k, v in original.heads.items()}
    tails = {k: v.copy() for k, v in original.tails.items()}
    rows = slice(10, 30)
    for arr in (heads["foot"], tails["shin"]):
        arr[rows, 0] *= keep
    heads["shin"][rows, 0] *= keep
    tails["thigh"][rows, 0] *= keep
    tails["foot"][rows, 0] *= keep
    return Samples(original.frame_start, heads, tails)


def test_slip_metrics() -> None:
    m = compute_metrics(leg(0.10), locked(leg(0.10), 0.1), CTX, 0)
    assert m.planted_displacement_cm_before == pytest.approx(10.0)
    assert m.planted_displacement_cm_after == pytest.approx(1.0)
    assert m.slip_reduction_pct == pytest.approx(90.0)
    assert m.collateral_max_cm == 0.0
    assert m.root_deviation_cm == 0.0
    assert m.joint_limit_violations == 0
    assert not m.gated
    assert m.objective_score is not None


def test_reduction_reported_as_zero_for_tiny_slides() -> None:
    assert compute_metrics(leg(0.004), leg(0.0), CTX, 0).slip_reduction_pct == 0.0


def test_contact_error_is_rms_about_the_mean() -> None:
    m = compute_metrics(leg(0.1), locked(leg(0.1), 0.0), CTX, 0)
    assert m.contact_error_cm == pytest.approx(0.0, abs=1e-12)
    moving = compute_metrics(leg(0.1), leg(0.1), CTX, 0)
    x = np.linspace(0, 0.1, 20)
    assert moving.contact_error_cm == pytest.approx(100 * np.sqrt(np.mean((x - x.mean()) ** 2)))


def test_penetration() -> None:
    cand = leg()
    cand.heads["foot"][20, 2] = 0.0  # ankle 2 cm below h_floor; the ball stays at 0.02
    m = compute_metrics(leg(), cand, CTX, 0)
    assert m.penetration_max_cm == pytest.approx(2.0)
    assert m.gated
    assert any("penetration" in r for r in m.gate_reasons)
    assert m.objective_score is None


def test_collateral_and_outside_window_gates() -> None:
    cand = leg()
    cand.heads["hand"][3] += [0.0, 0.0, 0.001]  # non-chain bone, outside the window
    m = compute_metrics(leg(), cand, CTX, 0)
    assert m.collateral_max_cm == pytest.approx(0.1)
    assert m.outside_window_max_cm == pytest.approx(0.1)
    assert m.gated
    assert len(m.gate_reasons) == 2


def test_chain_change_inside_window_is_not_collateral() -> None:
    cand = leg()
    cand.heads["shin"][15] += [0.01, 0.0, 0.0]
    m = compute_metrics(leg(), cand, CTX, 0)
    assert m.collateral_max_cm == 0.0
    assert m.outside_window_max_cm == 0.0
    assert m.target_deviation_rms_cm > 0


def test_root_deviation() -> None:
    cand = leg()
    cand.heads["pelvis"][5] += [0.0, 0.02, 0.0]
    assert compute_metrics(leg(), cand, CTX, 0).root_deviation_cm == pytest.approx(2.0)


def test_knee_flexion_and_violations() -> None:
    straight = leg(knee_bend=0.0)
    assert knee_flexion_deg(straight, "thigh", "shin") == pytest.approx(np.zeros(T), abs=1e-6)
    bent = leg(knee_bend=0.2)
    assert np.all(knee_flexion_deg(bent, "thigh", "shin") > 30)
    hyper = leg()
    hyper.tails["thigh"][20] = hyper.heads["thigh"][20] + [0, 0, 0.4]  # thigh points up: ~180°
    m = compute_metrics(leg(), hyper, CTX, 0)
    assert m.joint_limit_violations == 1
    assert compute_metrics(hyper, hyper, CTX, 0).joint_limit_violations == 0  # already violating


def test_jerk() -> None:
    p = np.zeros((10, 3))
    assert jerk_rms(p, 24) == 0.0
    p[:, 0] = np.arange(10) ** 3 / 1000.0  # constant third difference 6/1000 per frame^3
    assert jerk_rms(p, 24) == pytest.approx(0.006 * 24**3)
    assert jerk_rms(p[:3], 24) == 0.0


def _metrics(score_inputs: dict[str, float], *, gated: bool = False) -> CandidateMetrics:
    base = dict(
        planted_displacement_cm_before=10.0,
        planted_displacement_cm_after=1.0,
        slip_reduction_pct=90.0,
        contact_error_cm=0.0,
        penetration_max_cm=0.0,
        jerk_rms_ratio=1.0,
        joint_limit_violations=0,
        target_deviation_rms_cm=0.0,
        collateral_max_cm=0.0,
        outside_window_max_cm=0.0,
        root_deviation_cm=0.0,
        ik_unreachable_frames=0,
    )
    m = CandidateMetrics(**{**base, **score_inputs}, gated=gated)  # type: ignore[arg-type]
    return m.model_copy(update={"objective_score": None if gated else objective_score(m)})


def test_objective_score_formula() -> None:
    perfect = _metrics({"slip_reduction_pct": 100.0})
    assert perfect.objective_score == pytest.approx(1.0)
    m = _metrics(
        {
            "slip_reduction_pct": 50.0,
            "jerk_rms_ratio": 1.5,
            "target_deviation_rms_cm": 2.5,
            "contact_error_cm": 1.0,
        }
    )
    assert m.objective_score == pytest.approx(0.55 * 0.5 + 0.2 * 0.5 + 0.15 * 0.5 + 0.1 * 0.5)
    assert _metrics({"slip_reduction_pct": -20.0, "jerk_rms_ratio": 9.0}).objective_score == (
        pytest.approx(0.25)
    )


def test_ranking_orders_gated_last_and_breaks_ties() -> None:
    good = _metrics({"slip_reduction_pct": 95.0})
    better = _metrics({"slip_reduction_pct": 99.0})
    gated = _metrics({"slip_reduction_pct": 100.0}, gated=True)
    ranking = rank_candidates(
        [(CandidateLabel.A, "a" * 16, gated), (CandidateLabel.B, "b" * 16, good)]
    )
    assert [r.label for r in ranking] == [CandidateLabel.B, CandidateLabel.A]
    assert ranking[1].gated
    assert ranking[1].score is None
    ranking = rank_candidates(
        [(CandidateLabel.B, "b" * 16, good), (CandidateLabel.A, "a" * 16, better)]
    )
    assert ranking[0].label is CandidateLabel.A
    tie = rank_candidates([(CandidateLabel.B, "b" * 16, good), (CandidateLabel.A, "a" * 16, good)])
    assert [r.label for r in tie] == [CandidateLabel.A, CandidateLabel.B]
    closer = _metrics({"slip_reduction_pct": 95.0, "target_deviation_rms_cm": 0.0})
    farther = good.model_copy(update={"target_deviation_rms_cm": 1e-12})
    tie2 = rank_candidates(
        [(CandidateLabel.A, "a" * 16, farther), (CandidateLabel.B, "b" * 16, closer)]
    )
    assert tie2[0].label is CandidateLabel.B
