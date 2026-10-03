"""Shared primitives for every Kinesis contract.

Conventions (docs/ALGORITHMS.md):
- positions are in meters, in Blender world space, Z-up;
- frames are integers and ranges are inclusive;
- metrics are reported in centimeters, marked by the ``_cm`` suffix.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class KinesisModel(BaseModel):
    """Base class for all contracts: unknown fields are rejected and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
Vec3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
"""World-space vector in meters (Blender convention, Z-up)."""

Frame = Annotated[int, Field(ge=-1_048_574, le=1_048_574)]  # Blender's frame limits

# Blender caps datablock and bone names at 63 bytes. The character set is limited so
# that names are safe to log and to embed in filenames.
BlenderName = Annotated[
    str, StringConstraints(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_.\-: ]+$")
]

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{5,63}$")]
"""Server-generated id (job_id, scene_id, artifact_id)."""

Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CandidateId = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{16}$")]

ALGORITHM_VERSION = "foot_contact/1"
"""Bumped on any change to a formula or default in docs/ALGORITHMS.md. Part of candidate ids."""


class RepairType(StrEnum):
    FOOT_CONTACT = "FOOT_CONTACT"


class CandidateLabel(StrEnum):
    A = "A"
    B = "B"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_DECISION = "AWAITING_DECISION"
    APPLYING = "APPLYING"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset(
    {JobStatus.COMPLETED, JobStatus.REJECTED, JobStatus.FAILED, JobStatus.CANCELLED}
)

# Legal status transitions. The store rejects anything not listed here.
JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.AWAITING_DECISION,
            JobStatus.COMPLETED,  # no defect detected
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.AWAITING_DECISION: frozenset(
        {JobStatus.APPLYING, JobStatus.REJECTED, JobStatus.CANCELLED}
    ),
    JobStatus.APPLYING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.REJECTED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class CandidateStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ErrorCode(StrEnum):
    # Request validation (4xx)
    SCENE_NOT_FOUND = "SCENE_NOT_FOUND"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    CANDIDATE_NOT_FOUND = "CANDIDATE_NOT_FOUND"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARMATURE_NOT_FOUND = "ARMATURE_NOT_FOUND"
    BONE_NOT_FOUND = "BONE_NOT_FOUND"
    INVALID_FRAME_RANGE = "INVALID_FRAME_RANGE"
    UNSUPPORTED_REPAIR_TYPE = "UNSUPPORTED_REPAIR_TYPE"
    UNSUPPORTED_RIG = "UNSUPPORTED_RIG"
    INVALID_STATE = "INVALID_STATE"
    NOT_READY = "NOT_READY"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    FILE_INVALID = "FILE_INVALID"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    # Pipeline (recorded on jobs/candidates)
    PLAN_INVALID = "PLAN_INVALID"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    WORKER_CRASHED = "WORKER_CRASHED"
    WORKER_TIMEOUT = "WORKER_TIMEOUT"
    WORKER_OUTPUT_INVALID = "WORKER_OUTPUT_INVALID"
    RENDER_FAILED = "RENDER_FAILED"
    NO_VIABLE_CANDIDATE = "NO_VIABLE_CANDIDATE"
    STORAGE_ERROR = "STORAGE_ERROR"
    INTERNAL = "INTERNAL"


class ErrorInfo(KinesisModel):
    code: ErrorCode
    message: Annotated[str, StringConstraints(max_length=2000)]
    node: str | None = None
    retryable: bool = False


class TokenUsage(KinesisModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens
