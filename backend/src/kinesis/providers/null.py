"""NullProvider: no AI. The pipeline runs fully deterministically (KINESIS_PROVIDER=null)."""

from __future__ import annotations

from kinesis.providers.base import (
    EvaluationRequest,
    PlanRequest,
    ProviderHealth,
    ProviderResult,
    ResultStatus,
    VisualAnalysisRequest,
    VisualFindings,
)
from kinesis.repair.plan_validation import default_plan
from kinesis.schemas import RepairPlan, VisualEvaluation

_REASON = "no model provider configured"


class NullProvider:
    name = "null"

    async def plan_repair(self, req: PlanRequest) -> ProviderResult[RepairPlan]:
        return ProviderResult(
            status=ResultStatus.UNAVAILABLE,
            value=default_plan(req.selection, req.scope, _REASON),
            reasons=(_REASON,),
        )

    async def evaluate_candidate(self, req: EvaluationRequest) -> ProviderResult[VisualEvaluation]:
        return ProviderResult(status=ResultStatus.UNAVAILABLE, reasons=(_REASON,))

    async def analyze_visual(self, req: VisualAnalysisRequest) -> ProviderResult[VisualFindings]:
        return ProviderResult(status=ResultStatus.UNAVAILABLE, reasons=(_REASON,))

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(ok=True, detail="null provider (AI disabled)")
