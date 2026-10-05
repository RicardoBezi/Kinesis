"""Foot-slide detection (docs/ALGORITHMS.md §2), pure numpy.

The headline number is ``planted_displacement_cm``: how far the ankle drifts horizontally,
relative to where it touched down, while the foot is planted.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from kinesis.analysis.kinematics import Array
from kinesis.schemas.analysis import (
    AnimationFeatures,
    DefectReport,
    DetectionConfig,
    PlantedInterval,
    Severity,
)
from kinesis.schemas.common import ALGORITHM_VERSION

Mask = npt.NDArray[np.bool_]


@dataclass(frozen=True)
class ContactPlane:
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)
    normal: tuple[float, float, float] = (0.0, 0.0, 1.0)

    def arrays(self) -> tuple[Array, Array]:
        n = np.asarray(self.normal, dtype=np.float64)
        return np.asarray(self.origin, dtype=np.float64), n / np.linalg.norm(n)


@dataclass(frozen=True)
class Detection:
    features: AnimationFeatures
    report: DefectReport
    floor_height: float  # h_floor, needed again by the repair's height clamp


# ---------------------------------------------------------------- per-frame signals


def contact_height(foot: Array, toe: Array, plane: ContactPlane) -> Array:
    """Step 1: ``h[t] = min(dot(p - o, n), dot(q - o, n))``."""
    o, n = plane.arrays()
    out: Array = np.minimum((foot - o) @ n, (toe - o) @ n)
    return out


def project_to_plane(points: Array, plane: ContactPlane) -> Array:
    """Step 2: ``P(x) = x - dot(x - o, n) n``."""
    o, n = plane.arrays()
    out: Array = points - np.outer((points - o) @ n, n)
    return out


def horizontal_speed(u: Array, fps: float) -> Array:
    """Step 3: central differences inside, one-sided at both ends (m/s)."""
    if len(u) < 2:
        raise ValueError("need at least two frames")
    s = np.empty(len(u))
    s[1:-1] = np.linalg.norm(u[2:] - u[:-2], axis=1) * fps / 2
    s[0] = np.linalg.norm(u[1] - u[0]) * fps
    s[-1] = np.linalg.norm(u[-1] - u[-2]) * fps
    return s


# ---------------------------------------------------------------- mask cleanup


def runs(mask: Mask) -> list[tuple[int, int]]:
    """Maximal true runs as inclusive index pairs."""
    padded = np.concatenate(([False], np.asarray(mask, dtype=bool), [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b) - 1) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def close_gaps(mask: Mask, max_gap: int) -> Mask:
    """Step 6.1: fill false runs of length <= ``max_gap`` that have true on both sides."""
    out = np.array(mask, dtype=bool)
    for a, b in runs(~out):
        if a > 0 and b < len(out) - 1 and b - a + 1 <= max_gap:
            out[a : b + 1] = True
    return out


def remove_short_runs(mask: Mask, min_run: int) -> Mask:
    """Step 6.2: drop true runs shorter than ``min_run``."""
    out = np.array(mask, dtype=bool)
    for a, b in runs(out):
        if b - a + 1 < min_run:
            out[a : b + 1] = False
    return out


# ---------------------------------------------------------------- metrics


def planted_displacement_cm(u: Array, a: int, b: int) -> float:
    """``100 * max_{t in [a, b]} |u[t] - u[a]|``; ``a`` and ``b`` are array indices."""
    return float(100 * np.linalg.norm(u[a : b + 1] - u[a], axis=1).max())


def interval_metrics(
    u: Array,
    speed: Array,
    height: Array,
    a: int,
    b: int,
    frame_start: int,
    config: DetectionConfig,
) -> PlantedInterval:
    """Step 8 for the run at indices ``[a, b]``."""
    steps = np.linalg.norm(np.diff(u[a : b + 1], axis=0), axis=1)
    h = height[a : b + 1]
    consistent = np.abs(h - np.median(h)) <= config.consistency_band_m
    return PlantedInterval(
        start=frame_start + a,
        end=frame_start + b,
        anchor=(float(u[a, 0]), float(u[a, 1]), float(u[a, 2])),
        planted_displacement_cm=planted_displacement_cm(u, a, b),
        path_slip_cm=float(100 * steps.sum()),
        max_frame_slip_cm=float(100 * steps.max()) if len(steps) else 0.0,
        mean_slip_velocity_cm_s=float(100 * speed[a : b + 1].mean()),
        velocity_variance=float(speed[a : b + 1].var()),
        contact_consistency=float(consistent.mean()),
    )


def severity_for(displacement_cm: float, config: DetectionConfig) -> Severity:
    if displacement_cm < config.severity_minor_cm:
        return Severity.NONE
    if displacement_cm < config.severity_major_cm:
        return Severity.MINOR
    return Severity.MAJOR


# ---------------------------------------------------------------- entry point


def detect_foot_slide(
    foot: npt.ArrayLike,
    toe: npt.ArrayLike,
    *,
    frame_start: int,
    fps: float,
    selection: tuple[int, int],
    plane: ContactPlane | None = None,
    config: DetectionConfig | None = None,
    bone: str = "foot",
) -> Detection:
    """Detect planted intervals and measure slip over the context window.

    ``foot`` and ``toe`` are ``(T, 3)`` world positions of the ankle and the ball of the foot,
    where index ``i`` is frame ``frame_start + i``. ``selection`` is the animator's inclusive
    frame range; only intervals that intersect it are reported.
    """
    cfg = config or DetectionConfig()
    pl = plane or ContactPlane()
    p = np.array(foot, dtype=np.float64)
    q = np.array(toe, dtype=np.float64)
    if p.shape != q.shape or p.ndim != 2 or p.shape[1] != 3:
        raise ValueError("foot and toe must both have shape (T, 3)")

    h = contact_height(p, q, pl)
    u = project_to_plane(p, pl)
    s = horizontal_speed(u, fps)
    h_floor = float(np.percentile(h, cfg.floor_percentile))
    contact = (h - h_floor <= cfg.height_threshold_m) & (s <= cfg.speed_threshold_m_s)
    mask = remove_short_runs(close_gaps(contact, cfg.gap_close_frames), cfg.min_run_frames)

    sel_a, sel_b = selection[0] - frame_start, selection[1] - frame_start
    intervals = tuple(
        interval_metrics(u, s, h, a, b, frame_start, cfg)
        for a, b in runs(mask)
        if a <= sel_b and b >= sel_a
    )
    worst = (
        max(range(len(intervals)), key=lambda i: intervals[i].planted_displacement_cm)
        if intervals
        else None
    )
    severity = (
        severity_for(intervals[worst].planted_displacement_cm, cfg)
        if worst is not None
        else Severity.NONE
    )
    report = DefectReport(
        intervals=intervals,
        worst_interval_index=worst,
        severity=severity,
        summary=_summary(bone, intervals, worst, severity),
        detection_config=cfg,
        algorithm_version=ALGORITHM_VERSION,
    )
    features = AnimationFeatures(
        frame_start=frame_start,
        fps=fps,
        foot_pos=_vecs(p),
        toe_pos=_vecs(q),
        height=tuple(float(v) for v in h),
        horiz_speed=tuple(float(v) for v in s),
        planted_mask=tuple(bool(v) for v in mask),
    )
    return Detection(features=features, report=report, floor_height=h_floor)


def _vecs(a: Array) -> tuple[tuple[float, float, float], ...]:
    return tuple((float(x), float(y), float(z)) for x, y, z in a)


def _summary(
    bone: str, intervals: tuple[PlantedInterval, ...], worst: int | None, severity: Severity
) -> str:
    if worst is None:
        return f"{bone}: no planted contact found in the selected frames."
    w = intervals[worst]
    span = f"frames {w.start}-{w.end}"
    if severity is Severity.NONE:
        return (
            f"{bone}: planted on {span} with no measurable slide "
            f"({w.planted_displacement_cm:.2f} cm)."
        )
    return (
        f"{bone}: slides {w.planted_displacement_cm:.1f} cm while planted on {span} "
        f"({severity.value.lower()})."
    )


__all__ = [
    "ContactPlane",
    "Detection",
    "close_gaps",
    "contact_height",
    "detect_foot_slide",
    "horizontal_speed",
    "interval_metrics",
    "planted_displacement_cm",
    "project_to_plane",
    "remove_short_runs",
    "runs",
    "severity_for",
]
