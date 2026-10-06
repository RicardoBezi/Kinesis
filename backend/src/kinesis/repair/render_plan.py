"""Preview camera planning (docs/ALGORITHMS.md §5), pure numpy."""

from __future__ import annotations

import numpy as np

from kinesis.analysis.kinematics import Array
from kinesis.schemas.worker import RenderSpec

CROP_MARGIN_M = 0.35
MAX_RADIUS_M = 20.0


def crop_render_spec(
    ankle: Array,
    ball: Array,
    frame_start: int,
    window: tuple[int, int],
    context_range: tuple[int, int],
    *,
    margin: float = CROP_MARGIN_M,
) -> RenderSpec:
    """Aim the crop camera at the foot over ``window`` and render every context frame.

    ``ankle`` and ``ball`` are ``(T, 3)`` with index ``i`` = frame ``frame_start + i``.
    ``window`` is ``[a - k, b + k]``; it is clipped to the context range here.
    """
    lo = max(window[0], context_range[0]) - frame_start
    hi = min(window[1], context_range[1]) - frame_start
    if hi < lo:
        raise ValueError("crop window does not overlap the context range")
    points = np.concatenate([ankle[lo : hi + 1], ball[lo : hi + 1]])
    low = points.min(axis=0) - margin
    high = points.max(axis=0) + margin
    center = (low + high) / 2
    radius = float(min(np.max(high - low), MAX_RADIUS_M))
    return RenderSpec(
        crop_frames=context_range,
        crop_center=(float(center[0]), float(center[1]), float(center[2])),
        crop_radius_m=radius,
    )


__all__ = ["CROP_MARGIN_M", "crop_render_spec"]
