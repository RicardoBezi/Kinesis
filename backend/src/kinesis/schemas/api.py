"""HTTP request and response envelopes that are not domain aggregates."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from kinesis.schemas.common import (
    BlenderName,
    ErrorCode,
    Identifier,
    KinesisModel,
    RepairType,
    Sha256Hex,
)
from kinesis.schemas.job import JobEvent, PlanMode
from kinesis.schemas.scope import AnimationSelection


class BoneInfo(KinesisModel):
    name: BlenderName
    parent: BlenderName | None = None
    deform: bool = True


class ArmatureInfo(KinesisModel):
    name: BlenderName
    bones: tuple[BoneInfo, ...]


class SceneRef(KinesisModel):
    """An uploaded scene, inspected on upload so that job requests can be validated."""

    scene_id: Identifier
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    frame_start: int
    frame_end: int
    fps: float = Field(gt=0)
    blender_version: Annotated[str, StringConstraints(max_length=32)]
    armatures: tuple[ArmatureInfo, ...]


class CreateRepairJobRequest(KinesisModel):
    """The body of ``POST /v1/jobs``. Clients must also send an ``Idempotency-Key`` header."""

    repair_type: RepairType = RepairType.FOOT_CONTACT
    plan_mode: PlanMode = PlanMode.AUTO
    selection: AnimationSelection


class EventPage(KinesisModel):
    events: tuple[JobEvent, ...]
    next_seq: int = Field(ge=0, description="Pass as after_seq on the next poll")


class Problem(KinesisModel):
    """An RFC 7807 problem detail, extended with a machine-readable ``code``."""

    type: str = "about:blank"
    title: str
    status: int = Field(ge=400, le=599)
    code: ErrorCode
    detail: str | None = None
    instance: str | None = None
    errors: tuple[dict[str, str], ...] = ()


class ComponentHealth(KinesisModel):
    name: str
    ok: bool
    detail: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)


class HealthReport(KinesisModel):
    status: str  # "ok" | "degraded"
    version: str
    components: tuple[ComponentHealth, ...] = ()


class ProductStats(KinesisModel):
    """Product metrics computed from the store (docs/METRICS.md)."""

    jobs_total: int = Field(ge=0)
    jobs_decided: int = Field(ge=0)
    candidate_acceptance_rate: float | None = Field(default=None, ge=0, le=1)
    model_human_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    median_slip_reduction_pct: float | None = None
    median_time_to_decision_s: float | None = Field(default=None, ge=0)
    cost_per_accepted_repair_usd: float | None = Field(default=None, ge=0)


SceneId = Identifier
JobId = Identifier
