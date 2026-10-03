"""RepairPlan: the **only** model output that influences execution (ADR 0002).

Every field is either a closed enum or a bounded number. Validation happens in two steps:
1. structural validation here, with ``extra="forbid"`` and the field bounds;
2. semantic validation against the job's resolved scope in
   ``kinesis.repair.plan_validation``.
A plan that fails either step is never executed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from kinesis.schemas.common import BlenderName, CandidateLabel, Frame, KinesisModel, RepairType
from kinesis.schemas.scope import ContactTarget


class AnchorMode(StrEnum):
    ONSET = "ONSET"
    MEAN = "MEAN"


class CandidateParameters(KinesisModel):
    """Deterministic repair knobs (docs/ALGORITHMS.md §3.1)."""

    lock_strength: float = Field(ge=0.0, le=1.0)
    anchor_mode: AnchorMode
    blend_frames: int = Field(ge=0, le=24)
    tolerance_cm: float = Field(ge=0.0, le=5.0)
    lock_yaw: bool
    smoothing_window: Literal[0, 3, 5, 7, 9]
    height_clamp: bool = True


CANDIDATE_A_DEFAULT = CandidateParameters(
    lock_strength=1.0,
    anchor_mode=AnchorMode.ONSET,
    blend_frames=3,
    tolerance_cm=0.2,
    lock_yaw=True,
    smoothing_window=0,
    height_clamp=True,
)
CANDIDATE_B_DEFAULT = CandidateParameters(
    lock_strength=0.85,
    anchor_mode=AnchorMode.MEAN,
    blend_frames=8,
    tolerance_cm=1.0,
    lock_yaw=False,
    smoothing_window=5,
    height_clamp=True,
)
DEFAULT_CANDIDATES: dict[CandidateLabel, CandidateParameters] = {
    CandidateLabel.A: CANDIDATE_A_DEFAULT,
    CandidateLabel.B: CANDIDATE_B_DEFAULT,
}


class PlanSource(StrEnum):
    MODEL = "MODEL"
    FALLBACK = "FALLBACK"


class RepairPlan(KinesisModel):
    repair_type: RepairType
    target_bones: tuple[BlenderName, ...] = Field(min_length=1, max_length=8)
    frame_start: Frame
    frame_end: Frame
    context_frames_before: int = Field(ge=0, le=120)
    context_frames_after: int = Field(ge=0, le=120)
    contact_target: ContactTarget
    preserve_root_motion: Literal[True] = True
    preserve_non_target_bones: Literal[True] = True
    candidates: dict[CandidateLabel, CandidateParameters]
    explanation: Annotated[str, StringConstraints(max_length=2000)]
    plan_source: PlanSource
    model_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    fallback_reason: Annotated[str, StringConstraints(max_length=1000)] | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("frame_end must be >= frame_start")
        if set(self.candidates) != set(CandidateLabel):
            raise ValueError("plan must define exactly candidates A and B")
        if len(set(self.target_bones)) != len(self.target_bones):
            raise ValueError("target_bones must be unique")
        if self.plan_source is PlanSource.MODEL and self.model_id is None:
            raise ValueError("model plans must record model_id")
        if self.plan_source is PlanSource.FALLBACK and self.fallback_reason is None:
            raise ValueError("fallback plans must record fallback_reason")
        return self


class ModelPlanProposal(KinesisModel):
    """The subset of RepairPlan a model is allowed to author.

    The server fills in provenance and scope fields itself, so the model cannot claim to be
    a fallback, choose its own ``model_id``, or disable preservation.
    """

    repair_type: RepairType
    target_bones: tuple[BlenderName, ...] = Field(min_length=1, max_length=8)
    frame_start: Frame
    frame_end: Frame
    context_frames_before: int = Field(ge=0, le=120)
    context_frames_after: int = Field(ge=0, le=120)
    candidates: dict[CandidateLabel, CandidateParameters]
    explanation: Annotated[str, StringConstraints(max_length=2000)]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("frame_end must be >= frame_start")
        if set(self.candidates) != set(CandidateLabel):
            raise ValueError("plan must define exactly candidates A and B")
        if len(set(self.target_bones)) != len(self.target_bones):
            raise ValueError("target_bones must be unique")
        return self
