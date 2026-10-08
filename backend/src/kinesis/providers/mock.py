"""MockProvider: a scriptable stand-in for Token Factory (tests and offline demos).

Each call takes the next scripted behaviour; when the script runs out the default is used.
A behaviour is one of:
- an exception instance: raised (use ``kinesis.errors`` provider errors for transport faults);
- for ``plan_repair``, a string: raw model text, passed through the real validation gate
  ``accept_model_plan``, so malformed or out-of-scope plans become the fallback plan;
- for ``evaluate_candidate``, a ``ModelVisualJudgement`` (OK) or the string ``"invalid"``.

The default plan is a valid MODEL plan using the preset parameters; the default evaluation is
a neutral judgement. Nothing here touches the network.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable

from kinesis.providers.base import (
    EvaluationRequest,
    PlanRequest,
    ProviderHealth,
    ProviderResult,
    ResultStatus,
    VisualAnalysisRequest,
    VisualFindings,
)
from kinesis.repair.plan_validation import accept_model_plan
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    ModelVisualJudgement,
    RepairPlan,
    TokenUsage,
    VisualEvaluation,
)

PlanBehaviour = BaseException | str
EvalBehaviour = BaseException | ModelVisualJudgement | str

NEUTRAL = ModelVisualJudgement(
    contact_stability=4,
    naturalness=4,
    artifacts_visible=4,
    performance_preservation=4,
    instruction_adherence=4,
    notes="mock judgement",
    prefers_over_original=True,
)


def default_plan_text(req: PlanRequest) -> str:
    t = req.selection.temporal
    return json.dumps(
        {
            "repair_type": "FOOT_CONTACT",
            "target_bones": list(req.scope.target_bones),
            "frame_start": t.frame_start,
            "frame_end": t.frame_end,
            "context_frames_before": t.context_before,
            "context_frames_after": t.context_after,
            "candidates": {
                k.value: v.model_dump(mode="json") for k, v in DEFAULT_CANDIDATES.items()
            },
            "explanation": "mock planner: preset parameters",
        }
    )


class MockProvider:
    name = "mock"
    planner_model = "mock/planner"
    vision_model = "mock/vision"

    def __init__(
        self,
        plans: Iterable[PlanBehaviour] = (),
        evaluations: Iterable[EvalBehaviour] = (),
        *,
        healthy: bool = True,
    ) -> None:
        self._plans: deque[PlanBehaviour] = deque(plans)
        self._evals: deque[EvalBehaviour] = deque(evaluations)
        self._healthy = healthy
        self.plan_calls = 0
        self.eval_calls = 0
        self.eval_frame_counts: list[int] = []

    async def plan_repair(self, req: PlanRequest) -> ProviderResult[RepairPlan]:
        self.plan_calls += 1
        behaviour = self._plans.popleft() if self._plans else default_plan_text(req)
        if isinstance(behaviour, BaseException):
            raise behaviour
        decision = accept_model_plan(
            behaviour,
            model_id=self.planner_model,
            selection=req.selection,
            scope=req.scope,
            scene_range=req.scene_range,
        )
        return ProviderResult(
            status=ResultStatus.OK if decision.accepted else ResultStatus.INVALID,
            value=decision.plan,
            model_id=self.planner_model,
            usage=TokenUsage(prompt_tokens=800, completion_tokens=150),
            reasons=decision.rejection_reasons,
        )

    async def evaluate_candidate(self, req: EvaluationRequest) -> ProviderResult[VisualEvaluation]:
        self.eval_calls += 1
        self.eval_frame_counts.append(len(req.original_frames) + len(req.candidate_frames))
        behaviour = self._evals.popleft() if self._evals else NEUTRAL
        if isinstance(behaviour, BaseException):
            raise behaviour
        if isinstance(behaviour, str):
            return ProviderResult(
                status=ResultStatus.INVALID, model_id=self.vision_model, reasons=(behaviour,)
            )
        judgement = behaviour
        return ProviderResult(
            status=ResultStatus.OK,
            value=VisualEvaluation(
                candidate_id=req.candidate.candidate_id,
                contact_stability=judgement.contact_stability,
                naturalness=judgement.naturalness,
                artifacts_visible=judgement.artifacts_visible,
                performance_preservation=judgement.performance_preservation,
                instruction_adherence=judgement.instruction_adherence,
                notes=judgement.notes,
                model_id=self.vision_model,
            ),
            model_id=self.vision_model,
        )

    async def analyze_visual(self, req: VisualAnalysisRequest) -> ProviderResult[VisualFindings]:
        return ProviderResult(
            status=ResultStatus.OK, value=VisualFindings(answer="mock", confidence=0.5)
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            ok=self._healthy, detail="mock provider", models=(self.planner_model,)
        )


__all__ = ["NEUTRAL", "MockProvider", "default_plan_text"]
