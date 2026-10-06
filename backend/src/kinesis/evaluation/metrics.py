"""Objective candidate metrics and ranking (docs/ALGORITHMS.md §4, §4.1), pure numpy.

Every metric compares **original** and **candidate** world samples of all bones over the whole
scene. Slip is measured on the original interval ``[a, b]``; nothing is re-detected, so a
candidate cannot game detection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from kinesis.analysis.detection import ContactPlane, planted_displacement_cm, project_to_plane
from kinesis.analysis.kinematics import Array
from kinesis.schemas.candidate import CandidateMetrics
from kinesis.schemas.common import CandidateLabel
from kinesis.schemas.evaluation import ObjectiveRank
from kinesis.schemas.worker import BoneSamples

KNEE_LIMITS_DEG = (0.0, 150.0)
FLOOR = ContactPlane()
MIN_BEFORE_CM = 0.5

GATES = {
    "collateral_max_cm": 0.01,
    "outside_window_max_cm": 0.001,
    "joint_limit_violations": 0,
    "penetration_max_cm": 0.5,
}
SCORING_WEIGHTS = {"slip": 0.55, "jerk": 0.20, "deviation": 0.15, "contact": 0.10}


@dataclass(frozen=True)
class Samples:
    """World head/tail positions per bone, ``(T, 3)``, frame ``frame_start + i`` at index i."""

    frame_start: int
    heads: Mapping[str, Array]
    tails: Mapping[str, Array]

    @classmethod
    def from_worker(cls, frame_start: int, bones: Sequence[BoneSamples]) -> Samples:
        return cls(
            frame_start=frame_start,
            heads={b.name: np.array(b.head, dtype=np.float64) for b in bones},
            tails={b.name: np.array(b.tail, dtype=np.float64) for b in bones},
        )

    def ball(self, foot: str, toe: str | None) -> Array:
        return self.heads[toe] if toe is not None and toe in self.heads else self.tails[foot]


@dataclass(frozen=True)
class MetricContext:
    fps: float
    thigh: str
    shin: str
    foot: str
    toe: str | None
    chain_bones: frozenset[str]
    root_bones: tuple[str, ...]
    interval: tuple[int, int]
    blend_frames: int
    context_range: tuple[int, int]
    floor_height: float
    plane: ContactPlane = FLOOR


def _span(frame_start: int, count: int, lo: int, hi: int) -> slice:
    """Indices of frames ``[lo, hi]`` clipped to the samples."""
    a = max(lo - frame_start, 0)
    b = min(hi - frame_start, count - 1)
    return slice(a, max(a, b + 1))


def _rms(values: Array) -> float:
    return float(np.sqrt(np.mean(values * values))) if values.size else 0.0


def knee_flexion_deg(s: Samples, thigh: str, shin: str) -> Array:
    d1 = s.tails[thigh] - s.heads[thigh]
    d2 = s.tails[shin] - s.heads[shin]
    cos = np.einsum("ij,ij->i", d1, d2) / (np.linalg.norm(d1, axis=1) * np.linalg.norm(d2, axis=1))
    out: Array = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
    return out


def jerk_rms(p: Array, fps: float) -> float:
    if len(p) < 4:
        return 0.0
    j = np.diff(p, n=3, axis=0) * fps**3
    return _rms(np.linalg.norm(j, axis=1))


def _max_delta(
    orig: Samples, cand: Samples, bones: Sequence[str], rows: npt.NDArray[np.bool_]
) -> float:
    worst = 0.0
    for bone in bones:
        for o, c in ((orig.heads, cand.heads), (orig.tails, cand.tails)):
            if rows.any():
                worst = max(
                    worst, float(np.linalg.norm(c[bone][rows] - o[bone][rows], axis=1).max())
                )
    return worst


def compute_metrics(
    original: Samples, candidate: Samples, ctx: MetricContext, ik_unreachable_frames: int
) -> CandidateMetrics:
    if original.frame_start != candidate.frame_start:
        raise ValueError("samples must cover the same frames")
    count = len(original.heads[ctx.foot])
    fs = original.frame_start
    a, b = ctx.interval
    k = ctx.blend_frames
    ia, ib = a - fs, b - fs
    frames = np.arange(fs, fs + count)

    u = project_to_plane(original.heads[ctx.foot], ctx.plane)
    u2 = project_to_plane(candidate.heads[ctx.foot], ctx.plane)
    before = planted_displacement_cm(u, ia, ib)
    after = planted_displacement_cm(u2, ia, ib)
    reduction = 0.0 if before < MIN_BEFORE_CM else 100 * (before - after) / before

    planted = u2[ia : ib + 1]
    contact_error = 100 * _rms(np.linalg.norm(planted - planted.mean(axis=0), axis=1))

    o, n = ctx.plane.arrays()
    ctx_rows = _span(fs, count, *ctx.context_range)
    ball2 = candidate.ball(ctx.foot, ctx.toe)
    h2 = np.minimum((candidate.heads[ctx.foot] - o) @ n, (ball2 - o) @ n)[ctx_rows]
    penetration = 100 * float(np.maximum(0.0, ctx.floor_height - h2).max(initial=0.0))

    jerk_lo = max(a - k - 3, ctx.context_range[0])
    jerk_hi = min(b + k + 3, ctx.context_range[1])
    jr = _span(fs, count, jerk_lo, jerk_hi)
    j_orig = jerk_rms(original.heads[ctx.foot][jr], ctx.fps)
    j_cand = jerk_rms(candidate.heads[ctx.foot][jr], ctx.fps)
    jerk_ratio = j_cand / max(j_orig, 1e-9)

    flex_o = knee_flexion_deg(original, ctx.thigh, ctx.shin)
    flex_c = knee_flexion_deg(candidate, ctx.thigh, ctx.shin)
    lo, hi = KNEE_LIMITS_DEG
    violations = int(np.sum(((flex_c < lo) | (flex_c > hi)) & ~((flex_o < lo) | (flex_o > hi))))

    window = _span(fs, count, a - k, b + k)
    joints = (
        (original.heads[ctx.shin], candidate.heads[ctx.shin]),
        (original.heads[ctx.foot], candidate.heads[ctx.foot]),
        (original.ball(ctx.foot, ctx.toe), ball2),
    )
    deviation = 100 * _rms(
        np.concatenate([np.linalg.norm(c[window] - o_[window], axis=1) for o_, c in joints])
    )

    all_rows = np.ones(count, dtype=bool)
    outside = (frames < a - k) | (frames > b + k)
    others = [bn for bn in original.heads if bn not in ctx.chain_bones]
    collateral = 100 * _max_delta(original, candidate, others, all_rows)
    outside_max = 100 * _max_delta(original, candidate, list(original.heads), outside)
    roots = [bn for bn in ctx.root_bones if bn in original.heads]
    root_dev = 100 * max(
        (
            float(np.linalg.norm(candidate.heads[r] - original.heads[r], axis=1).max())
            for r in roots
        ),
        default=0.0,
    )

    values = {
        "collateral_max_cm": collateral,
        "outside_window_max_cm": outside_max,
        "joint_limit_violations": violations,
        "penetration_max_cm": penetration,
    }
    reasons = tuple(
        f"{name} {values[name]:.4g} > {limit}"
        for name, limit in GATES.items()
        if values[name] > limit
    )
    metrics = CandidateMetrics(
        planted_displacement_cm_before=before,
        planted_displacement_cm_after=after,
        slip_reduction_pct=reduction,
        contact_error_cm=contact_error,
        penetration_max_cm=penetration,
        jerk_rms_ratio=jerk_ratio,
        joint_limit_violations=violations,
        target_deviation_rms_cm=deviation,
        collateral_max_cm=collateral,
        outside_window_max_cm=outside_max,
        root_deviation_cm=root_dev,
        ik_unreachable_frames=ik_unreachable_frames,
        gated=bool(reasons),
        gate_reasons=reasons,
    )
    return metrics.model_copy(
        update={"objective_score": None if reasons else objective_score(metrics)}
    )


def objective_score(m: CandidateMetrics) -> float:
    w = SCORING_WEIGHTS
    score = (
        w["slip"] * float(np.clip(m.slip_reduction_pct / 100, 0, 1))
        + w["jerk"] * (1 - float(np.clip(m.jerk_rms_ratio - 1, 0, 1)))
        + w["deviation"] * (1 - float(np.clip(m.target_deviation_rms_cm / 5, 0, 1)))
        + w["contact"] * (1 - float(np.clip(m.contact_error_cm / 2, 0, 1)))
    )
    return min(1.0, max(0.0, score))


def rank_candidates(
    candidates: Sequence[tuple[CandidateLabel, str, CandidateMetrics]],
) -> tuple[ObjectiveRank, ...]:
    """§4.1: ungated by score (desc), ties by lower deviation then label; gated last."""

    def key(item: tuple[CandidateLabel, str, CandidateMetrics]) -> tuple[int, float, float, str]:
        label, _, m = item
        score = m.objective_score if m.objective_score is not None else -1.0
        return (int(m.gated), -score, m.target_deviation_rms_cm, label.value)

    return tuple(
        ObjectiveRank(label=label, candidate_id=cid, score=m.objective_score, gated=m.gated)
        for label, cid, m in sorted(candidates, key=key)
    )


__all__ = [
    "GATES",
    "SCORING_WEIGHTS",
    "MetricContext",
    "Samples",
    "compute_metrics",
    "jerk_rms",
    "knee_flexion_deg",
    "objective_score",
    "rank_candidates",
]
