"""Evaluation and human decision contracts (docs/ALGORITHMS.md §4.1, PRODUCT.md)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import Field, StringConstraints, model_validator

from kinesis.schemas.common import (
    CandidateId,
    CandidateLabel,
    Identifier,
    KinesisModel,
    TokenUsage,
)

Score = Annotated[int, Field(ge=1, le=5)]


class VisualEvaluation(KinesisModel):
    """A structured multimodal judgement of one candidate against the original.

    Scores run from 1 (bad) to 5 (good). For ``artifacts_visible``, 5 means *no* visible
    artifacts.
    """

    candidate_id: CandidateId
    contact_stability: Score
    naturalness: Score
    artifacts_visible: Score
    performance_preservation: Score
    instruction_adherence: Score
    notes: Annotated[str, StringConstraints(max_length=1000)] = ""
    prefers_over_original: bool | None = None
    model_id: Annotated[str, StringConstraints(max_length=200)]
    usage: TokenUsage = TokenUsage()
    latency_ms: int = Field(default=0, ge=0)


class ModelVisualJudgement(KinesisModel):
    """The subset of VisualEvaluation a model authors. The server adds ids, usage and timing."""

    contact_stability: Score
    naturalness: Score
    artifacts_visible: Score
    performance_preservation: Score
    instruction_adherence: Score
    notes: Annotated[str, StringConstraints(max_length=1000)] = ""
    prefers_over_original: bool


class EvaluatorStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"  # the provider failed for some or all candidates; objective-only
    SKIPPED = "SKIPPED"  # no provider configured


class ObjectiveRank(KinesisModel):
    label: CandidateLabel
    candidate_id: CandidateId
    score: float | None = Field(default=None, ge=0, le=1)
    gated: bool


class EvaluationReport(KinesisModel):
    objective_ranking: tuple[ObjectiveRank, ...]
    visual: tuple[VisualEvaluation, ...] = ()
    recommended: CandidateLabel | None = None
    recommendation_reason: Annotated[str, StringConstraints(max_length=2000)] = ""
    evaluator_status: EvaluatorStatus
    scoring_weights: dict[str, float] = Field(default_factory=dict)


class DecisionChoice(StrEnum):
    A = "A"
    B = "B"
    REJECT_ALL = "REJECT_ALL"


class DecisionRequest(KinesisModel):
    """The body of ``POST /v1/jobs/{job_id}/decision``."""

    choice: DecisionChoice
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None
    time_to_decision_s: float | None = Field(default=None, ge=0, le=86_400)


class HumanDecision(KinesisModel):
    """The stored decision. Comparing it with the model's recommendation gives the
    agreement metric."""

    job_id: Identifier
    choice: DecisionChoice
    candidate_id: CandidateId | None = None
    model_recommended: CandidateLabel | None = None
    decided_at: datetime
    time_to_decision_s: float | None = Field(default=None, ge=0)
    note: Annotated[str, StringConstraints(max_length=1000)] | None = None

    agreed: bool | None = Field(
        default=None, description="choice == model_recommended; null when no recommendation"
    )

    @model_validator(mode="before")
    @classmethod
    def _derive_agreed(cls, data: Any) -> Any:
        """The server derives ``agreed``. A value supplied by the caller is overwritten."""
        if isinstance(data, dict):
            choice, rec = data.get("choice"), data.get("model_recommended")
            data = {**data, "agreed": None if rec is None else str(choice) == str(rec)}
        return data
