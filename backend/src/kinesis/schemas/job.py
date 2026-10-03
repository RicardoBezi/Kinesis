"""RepairJob aggregate and its event log (ADR 0006)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, JsonValue, StringConstraints

from kinesis.schemas.analysis import DefectReport
from kinesis.schemas.candidate import ArtifactReference, RepairCandidate
from kinesis.schemas.common import (
    CandidateId,
    ErrorInfo,
    Identifier,
    JobStatus,
    KinesisModel,
    RepairType,
)
from kinesis.schemas.evaluation import EvaluationReport, HumanDecision
from kinesis.schemas.plan import RepairPlan
from kinesis.schemas.scope import AnimationSelection, SkeletalScope

IdempotencyKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{8,128}$")]


class PlanMode(StrEnum):
    AUTO = "AUTO"  # ask the configured provider; fall back to deterministic presets
    DETERMINISTIC = "DETERMINISTIC"  # never call a model for planning


class RepairJob(KinesisModel):
    job_id: Identifier
    idempotency_key: IdempotencyKey
    repair_type: RepairType
    plan_mode: PlanMode
    selection: AnimationSelection
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    skeletal_scope: SkeletalScope | None = None
    defect: DefectReport | None = None
    plan: RepairPlan | None = None
    candidates: tuple[RepairCandidate, ...] = ()
    evaluation: EvaluationReport | None = None
    decision: HumanDecision | None = None
    output: ArtifactReference | None = None
    error: ErrorInfo | None = None
    last_event_seq: int = Field(default=0, ge=0)


class JobEventType(StrEnum):
    STATUS_CHANGED = "STATUS_CHANGED"
    NODE_STARTED = "NODE_STARTED"
    NODE_SUCCEEDED = "NODE_SUCCEEDED"
    NODE_FAILED = "NODE_FAILED"
    NODE_SKIPPED = "NODE_SKIPPED"
    NODE_RETRY = "NODE_RETRY"
    CANDIDATE_UPDATED = "CANDIDATE_UPDATED"
    PLAN_REJECTED = "PLAN_REJECTED"  # a model plan failed validation and the fallback was used


class JobEvent(KinesisModel):
    """An append-only progress event. ``(job_id, seq)`` is unique and ``seq`` starts at 1."""

    seq: int = Field(ge=1)
    job_id: Identifier
    ts: datetime
    type: JobEventType
    node: Annotated[str, StringConstraints(max_length=64)] | None = None
    candidate_id: CandidateId | None = None
    attempt: int | None = Field(default=None, ge=1)
    message: Annotated[str, StringConstraints(max_length=1000)] = ""
    data: dict[str, JsonValue] = Field(default_factory=dict)
