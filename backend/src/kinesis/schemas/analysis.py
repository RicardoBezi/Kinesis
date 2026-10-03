"""Deterministic analysis outputs (docs/ALGORITHMS.md §2)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from kinesis.schemas.common import FiniteFloat, Frame, KinesisModel, Vec3


class DetectionConfig(KinesisModel):
    """Detection thresholds. Defaults are frozen under ALGORITHM_VERSION."""

    height_threshold_m: float = Field(default=0.025, gt=0, le=0.5)
    speed_threshold_m_s: float = Field(default=0.6, gt=0, le=10)
    floor_percentile: float = Field(default=5.0, ge=0, le=50)
    gap_close_frames: int = Field(default=2, ge=0, le=12)
    min_run_frames: int = Field(default=6, ge=1, le=120)
    consistency_band_m: float = Field(default=0.01, gt=0, le=0.2)
    severity_minor_cm: float = Field(default=0.5, ge=0)
    severity_major_cm: float = Field(default=2.0, ge=0)


class AnimationFeatures(KinesisModel):
    """Per-frame foot features over the context window.

    Inside a job this is stored as a FEATURES_JSON artifact and is not inlined into API
    responses.
    """

    frame_start: Frame
    fps: float = Field(gt=0, le=1000)
    foot_pos: tuple[Vec3, ...] = Field(min_length=2)
    toe_pos: tuple[Vec3, ...] = Field(min_length=2)
    height: tuple[FiniteFloat, ...]
    horiz_speed: tuple[FiniteFloat, ...]
    planted_mask: tuple[bool, ...]

    @model_validator(mode="after")
    def _same_length(self) -> Self:
        n = len(self.foot_pos)
        lengths = {
            len(self.toe_pos),
            len(self.height),
            len(self.horiz_speed),
            len(self.planted_mask),
        }
        if lengths != {n}:
            raise ValueError("all per-frame arrays must have the same length")
        return self

    @property
    def frame_end(self) -> int:
        return self.frame_start + len(self.foot_pos) - 1


class PlantedInterval(KinesisModel):
    start: Frame
    end: Frame
    anchor: Vec3
    planted_displacement_cm: float = Field(ge=0)
    path_slip_cm: float = Field(ge=0)
    max_frame_slip_cm: float = Field(ge=0)
    mean_slip_velocity_cm_s: float = Field(ge=0)
    velocity_variance: float = Field(ge=0)
    contact_consistency: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _order(self) -> Self:
        if self.end < self.start:
            raise ValueError("interval end must be >= start")
        return self


class Severity(StrEnum):
    NONE = "NONE"
    MINOR = "MINOR"
    MAJOR = "MAJOR"


class DefectReport(KinesisModel):
    intervals: tuple[PlantedInterval, ...]
    worst_interval_index: int | None = Field(default=None, ge=0)
    severity: Severity
    summary: Annotated[str, StringConstraints(max_length=1000)]
    detection_config: DetectionConfig = DetectionConfig()
    algorithm_version: str

    @model_validator(mode="after")
    def _worst(self) -> Self:
        if self.intervals:
            if self.worst_interval_index is None or self.worst_interval_index >= len(
                self.intervals
            ):
                raise ValueError("worst_interval_index must index into intervals")
        elif self.worst_interval_index is not None or self.severity is not Severity.NONE:
            raise ValueError("an empty report must have severity NONE and no worst interval")
        return self

    @property
    def worst(self) -> PlantedInterval | None:
        if self.worst_interval_index is None:
            return None
        return self.intervals[self.worst_interval_index]
