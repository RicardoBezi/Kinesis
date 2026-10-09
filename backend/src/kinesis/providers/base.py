"""ModelProvider: the only interface the pipeline uses to reach an AI model.

Nothing outside ``kinesis.providers`` knows a model id, a base URL or a prompt. Every
result is validated data, and nothing a provider returns is ever executed (ADR 0002).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from kinesis.schemas import (
    AnimationSelection,
    CandidateMetrics,
    DefectReport,
    RepairCandidate,
    RepairPlan,
    SkeletalScope,
    TokenUsage,
    VisualEvaluation,
)


@dataclass(frozen=True, slots=True)
class PlanRequest:
    selection: AnimationSelection
    scope: SkeletalScope
    defect: DefectReport
    scene_range: tuple[int, int]


@dataclass(frozen=True, slots=True)
class EvaluationRequest:
    selection: AnimationSelection
    candidate: RepairCandidate
    metrics: CandidateMetrics
    original_frames: tuple[Path, ...]  # sampled crop frames (same frame numbers for both)
    candidate_frames: tuple[Path, ...]
    frame_numbers: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class VisualAnalysisRequest:
    """Free-form visual question about rendered frames (e.g. 'is the foot visibly sliding?')."""

    frames: tuple[Path, ...]
    question: str


@dataclass(frozen=True, slots=True)
class VisualFindings:
    answer: str
    confidence: float  # 0..1, model-reported; advisory only


class ResultStatus(StrEnum):
    OK = "OK"
    INVALID = "INVALID"  # model answered but output failed validation
    UNAVAILABLE = "UNAVAILABLE"  # provider not configured / failed after retries


@dataclass(frozen=True, slots=True)
class ProviderResult[T]:
    status: ResultStatus
    value: T | None = None
    model_id: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    latency_ms: int = 0
    reasons: tuple[str, ...] = ()
    cost_usd: float | None = None  # from catalog prices; None until they are known (C9)

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.OK and self.value is not None


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    ok: bool
    detail: str
    models: tuple[str, ...] = ()


@runtime_checkable
class ModelProvider(Protocol):
    """Implementations: NullProvider, MockProvider (Phase 3), TokenFactoryProvider (Phase 4).

    Contract:
    - never raises for model-quality problems; returns INVALID with reasons instead;
    - raises only ``kinesis.errors.ProviderError`` subclasses for transport failures, so the
      orchestrator's retry policy and circuit breaker can classify them;
    - ``plan_repair`` returns a plan that has ALREADY passed
      ``kinesis.repair.plan_validation.accept_model_plan``; on INVALID the caller uses the
      fallback plan carried in ``value`` (plan_source=FALLBACK).
    """

    name: str

    async def plan_repair(self, req: PlanRequest) -> ProviderResult[RepairPlan]: ...

    async def evaluate_candidate(
        self, req: EvaluationRequest
    ) -> ProviderResult[VisualEvaluation]: ...

    async def analyze_visual(
        self, req: VisualAnalysisRequest
    ) -> ProviderResult[VisualFindings]: ...

    async def health_check(self) -> ProviderHealth: ...
