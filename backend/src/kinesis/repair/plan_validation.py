"""The gate between model output and execution (ADR 0002).

``accept_model_plan`` is the only route from raw provider text to an executable
``RepairPlan``. Any failure, whether structural or semantic, produces the deterministic
fallback plan along with a recorded reason. An invalid plan is never executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import ValidationError

from kinesis.schemas.common import RepairType
from kinesis.schemas.plan import (
    DEFAULT_CANDIDATES,
    ModelPlanProposal,
    PlanSource,
    RepairPlan,
)
from kinesis.schemas.scope import AnimationSelection, SkeletalScope

SUPPORTED_REPAIR_TYPES = frozenset({RepairType.FOOT_CONTACT})


@dataclass(frozen=True, slots=True)
class PlanDecision:
    plan: RepairPlan
    accepted: bool
    rejection_reasons: tuple[str, ...] = ()


def semantic_errors(
    proposal: ModelPlanProposal,
    selection: AnimationSelection,
    scope: SkeletalScope,
    scene_range: tuple[int, int],
) -> list[str]:
    """Return the reasons a structurally valid proposal is not executable. Empty means OK."""
    errors: list[str] = []
    if proposal.repair_type not in SUPPORTED_REPAIR_TYPES:
        errors.append(f"unsupported repair_type {proposal.repair_type}")
    unknown = sorted(set(proposal.target_bones) - set(scope.chain_bones))
    if unknown:
        errors.append(f"bones outside skeletal scope: {unknown}")
    lo, hi = selection.temporal.clipped(*scene_range)
    if proposal.frame_start < lo or proposal.frame_end > hi:
        errors.append(
            f"frames [{proposal.frame_start}, {proposal.frame_end}] outside context [{lo}, {hi}]"
        )
    return errors


def default_plan(selection: AnimationSelection, scope: SkeletalScope, reason: str) -> RepairPlan:
    """The deterministic plan: candidates A and B use the frozen presets."""
    return RepairPlan(
        repair_type=RepairType.FOOT_CONTACT,
        target_bones=scope.target_bones,
        frame_start=selection.temporal.frame_start,
        frame_end=selection.temporal.frame_end,
        context_frames_before=selection.temporal.context_before,
        context_frames_after=selection.temporal.context_after,
        contact_target=selection.contact,
        candidates=dict(DEFAULT_CANDIDATES),
        explanation=(
            "Deterministic presets: A locks the foot at touchdown with short blends; "
            "B softly pulls the foot toward its mean planted position, preserving more "
            "of the original trajectory."
        ),
        plan_source=PlanSource.FALLBACK,
        fallback_reason=reason[:1000],
    )


def accept_model_plan(
    raw: str | None,
    *,
    model_id: str,
    selection: AnimationSelection,
    scope: SkeletalScope,
    scene_range: tuple[int, int],
) -> PlanDecision:
    """Parse and validate raw model output. Always returns an executable plan."""
    if raw is None or not raw.strip():
        return _reject(selection, scope, ["empty model response"])
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _reject(selection, scope, [f"malformed JSON: {exc.msg}"])
    try:
        proposal = ModelPlanProposal.model_validate(payload)
    except ValidationError as exc:
        reasons = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10]]
        return _reject(selection, scope, reasons)
    errors = semantic_errors(proposal, selection, scope, scene_range)
    if errors:
        return _reject(selection, scope, errors)
    try:
        plan = RepairPlan(
            repair_type=proposal.repair_type,
            target_bones=proposal.target_bones,
            frame_start=proposal.frame_start,
            frame_end=proposal.frame_end,
            context_frames_before=proposal.context_frames_before,
            context_frames_after=proposal.context_frames_after,
            contact_target=selection.contact,  # never taken from the model
            candidates=proposal.candidates,
            explanation=proposal.explanation,
            plan_source=PlanSource.MODEL,
            model_id=model_id,
        )
    except ValidationError as exc:  # defense in depth: the full plan has stricter rules
        return _reject(selection, scope, [e["msg"] for e in exc.errors()[:10]])
    return PlanDecision(plan=plan, accepted=True)


def _reject(
    selection: AnimationSelection, scope: SkeletalScope, reasons: list[str]
) -> PlanDecision:
    return PlanDecision(
        plan=default_plan(selection, scope, "; ".join(reasons)),
        accepted=False,
        rejection_reasons=tuple(reasons),
    )
