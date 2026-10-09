"""The model-plan gate: invalid output must always become the deterministic fallback."""

from __future__ import annotations

import json
from typing import Any

import pytest

from kinesis.repair.plan_validation import accept_model_plan, default_plan
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    AnimationSelection,
    PlanSource,
    SkeletalScope,
)


def _valid_payload() -> dict[str, Any]:
    return {
        "repair_type": "FOOT_CONTACT",
        "target_bones": ["foot.L"],
        "frame_start": 45,
        "frame_end": 90,
        "context_frames_before": 10,
        "context_frames_after": 10,
        "candidates": {
            "A": DEFAULT_CANDIDATES["A"].model_dump(mode="json"),  # type: ignore[index]
            "B": {**DEFAULT_CANDIDATES["B"].model_dump(mode="json"), "lock_strength": 0.8},  # type: ignore[index]
        },
        "explanation": "Foot planted 40-90 drifts 10 cm in +X; lock at touchdown.",
    }


def _accept(
    raw: str | None, selection: AnimationSelection, scope: SkeletalScope
) -> tuple[bool, PlanSource, tuple[str, ...]]:
    d = accept_model_plan(
        raw, model_id="test/model", selection=selection, scope=scope, scene_range=(1, 120)
    )
    return d.accepted, d.plan.plan_source, d.rejection_reasons


def test_valid_plan_is_accepted(selection: AnimationSelection, scope: SkeletalScope) -> None:
    d = accept_model_plan(
        json.dumps(_valid_payload()),
        model_id="test/model",
        selection=selection,
        scope=scope,
        scene_range=(1, 120),
    )
    assert d.accepted
    assert d.plan.plan_source is PlanSource.MODEL
    assert d.plan.model_id == "test/model"
    assert d.plan.contact_target == selection.contact
    assert d.plan.candidates["B"].lock_strength == 0.8  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutate", "reason_fragment"),
    [
        (lambda p: p.update(target_bones=["hand.R"]), "outside skeletal scope"),
        (lambda p: p.update(target_bones=["foot.L; import os"]), "target_bones"),
        (lambda p: p.update(frame_start=1, frame_end=200), "outside context"),
        (lambda p: p.update(repair_type="FULL_BODY_REGEN"), "repair_type"),
        (lambda p: p.update(python="import os; os.system('x')"), "Extra inputs"),
        (lambda p: p["candidates"]["A"].update(lock_strength=3.0), "lock_strength"),
        (lambda p: p["candidates"].pop("B"), "exactly candidates A and B"),
        (lambda p: p.update(plan_source="FALLBACK"), "Extra inputs"),
        (lambda p: p.update(preserve_non_target_bones=False), "Extra inputs"),
    ],
)
def test_invalid_plans_fall_back(
    selection: AnimationSelection,
    scope: SkeletalScope,
    mutate: Any,
    reason_fragment: str,
) -> None:
    payload = _valid_payload()
    mutate(payload)
    accepted, source, reasons = _accept(json.dumps(payload), selection, scope)
    assert not accepted
    assert source is PlanSource.FALLBACK
    assert any(reason_fragment in r for r in reasons), reasons


@pytest.mark.parametrize("raw", [None, "", "   ", "{not json", "[]", "null", '"plan"'])
def test_garbage_falls_back(
    raw: str | None, selection: AnimationSelection, scope: SkeletalScope
) -> None:
    accepted, source, reasons = _accept(raw, selection, scope)
    assert not accepted
    assert source is PlanSource.FALLBACK
    assert reasons


def test_default_plan_uses_presets(selection: AnimationSelection, scope: SkeletalScope) -> None:
    plan = default_plan(selection, scope, "no provider")
    assert plan.candidates == DEFAULT_CANDIDATES
    assert plan.fallback_reason == "no provider"
    assert (plan.frame_start, plan.frame_end) == (45, 90)


def test_model_plan_with_a_weak_candidate_falls_back(
    selection: AnimationSelection, scope: SkeletalScope
) -> None:
    """2026-10-09 benchmark: a lock_strength of 0.4 left 6.1 cm of slide, so it is rejected."""
    import json

    from kinesis.repair.plan_validation import MIN_MODEL_LOCK_STRENGTH, accept_model_plan
    from kinesis.schemas import DEFAULT_CANDIDATES, CandidateLabel, PlanSource

    candidates = {k.value: v.model_dump(mode="json") for k, v in DEFAULT_CANDIDATES.items()}
    candidates["B"]["lock_strength"] = 0.4
    raw = json.dumps(
        {
            "repair_type": "FOOT_CONTACT", "target_bones": ["foot.L"], "frame_start": 45,
            "frame_end": 90, "context_frames_before": 10, "context_frames_after": 10,
            "candidates": candidates, "explanation": "B preserves more of the original",
        }
    )  # fmt: skip
    decision = accept_model_plan(
        raw, model_id="m", selection=selection, scope=scope, scene_range=(1, 120)
    )
    assert not decision.accepted
    assert decision.plan.plan_source is PlanSource.FALLBACK
    assert any(
        "candidate B" in r and str(MIN_MODEL_LOCK_STRENGTH) in r for r in decision.rejection_reasons
    )
    assert decision.plan.candidates[CandidateLabel.B].lock_strength == 0.85
