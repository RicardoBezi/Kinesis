"""Repair candidates, their artifacts and objective metrics."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints

from kinesis.schemas.common import (
    ALGORITHM_VERSION,
    CandidateId,
    CandidateLabel,
    CandidateStatus,
    ErrorInfo,
    Identifier,
    KinesisModel,
    Sha256Hex,
)
from kinesis.schemas.plan import CandidateParameters


class ArtifactKind(StrEnum):
    PREVIEW_FRAMES = "PREVIEW_FRAMES"  # crop camera frame sequence (frame_count frames)
    CONTEXT_FRAMES = "CONTEXT_FRAMES"  # wide camera, every 4th frame
    FEATURES_JSON = "FEATURES_JSON"
    SAMPLES_JSON = "SAMPLES_JSON"  # all-bone world samples (global verification)
    METRICS_JSON = "METRICS_JSON"
    CANDIDATE_BLEND = "CANDIDATE_BLEND"
    OUTPUT_BLEND = "OUTPUT_BLEND"
    LOG = "LOG"


class ArtifactReference(KinesisModel):
    """A file the server produced. Clients address it only by ``artifact_id``.

    A frame sequence is fetched one frame at a time with ``GET {uri}?frame=<index>``,
    where 0 <= index < frame_count.
    """

    artifact_id: Identifier
    kind: ArtifactKind
    media_type: Annotated[str, StringConstraints(max_length=100)]
    sha256: Sha256Hex
    size_bytes: int = Field(ge=0)
    frame_count: int | None = Field(default=None, ge=1)
    first_frame: int | None = None
    frame_step: int | None = Field(default=None, ge=1)
    uri: Annotated[str, StringConstraints(pattern=r"^/v1/artifacts/[a-z0-9_-]+$")]


class CandidateMetrics(KinesisModel):
    """Objective metrics (docs/ALGORITHMS.md §4). Slip is measured on the ORIGINAL interval."""

    planted_displacement_cm_before: float = Field(ge=0)
    planted_displacement_cm_after: float = Field(ge=0)
    slip_reduction_pct: float = Field(le=100)
    contact_error_cm: float = Field(ge=0)
    penetration_max_cm: float = Field(ge=0)
    jerk_rms_ratio: float = Field(ge=0)
    joint_limit_violations: int = Field(ge=0)
    target_deviation_rms_cm: float = Field(ge=0)
    collateral_max_cm: float = Field(ge=0)
    outside_window_max_cm: float = Field(ge=0)
    root_deviation_cm: float = Field(ge=0)
    ik_unreachable_frames: int = Field(ge=0)
    gated: bool = False
    gate_reasons: tuple[str, ...] = ()
    objective_score: float | None = Field(default=None, ge=0, le=1)


class RepairCandidate(KinesisModel):
    candidate_id: CandidateId
    label: CandidateLabel
    parameters: CandidateParameters
    algorithm_version: str = ALGORITHM_VERSION
    status: CandidateStatus = CandidateStatus.PENDING
    artifacts: tuple[ArtifactReference, ...] = ()
    metrics: CandidateMetrics | None = None
    error: ErrorInfo | None = None


def canonical_json(value: object) -> str:
    """Canonical JSON: sorted keys, no whitespace. Floats use repr, which is Python's default."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def compute_candidate_id(
    input_hash: str,
    parameters: CandidateParameters,
    algorithm_version: str = ALGORITHM_VERSION,
) -> str:
    """Return a deterministic candidate id (docs/ALGORITHMS.md §3.4).

    The same input hash, parameters and algorithm version always give the same id.
    """
    payload = "|".join(
        (input_hash, canonical_json(parameters.model_dump(mode="json")), algorithm_version)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
