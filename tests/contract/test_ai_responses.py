"""AI contract tests run against synthetic responses (tests/fixtures/ai_responses).

Phase 0 checks the validation gate: every model output that should not run becomes the
deterministic fallback plan, or a DEGRADED evaluation. Phase 4 adds transport tests that
replay the same files through ``httpx.MockTransport`` into ``TokenFactoryProvider``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kinesis.repair.plan_validation import accept_model_plan
from kinesis.schemas import (
    AnimationSelection,
    ModelVisualJudgement,
    PlanSource,
    SkeletalScope,
)

pytestmark = pytest.mark.contract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ai_responses"
ALL = sorted(p.name for p in FIXTURES.glob("*.json"))
EXPECTATIONS = {
    "accepted",
    "accepted_after_fence_strip",
    "fallback",
    "degraded",
    "retryable_error",
    "fatal_error",
}


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def content_of(case: dict[str, Any]) -> str | None:
    choices = case["body"].get("choices") or []
    if not choices:
        return None
    content: str | None = choices[0]["message"].get("content")
    return content


def cases(task: str, *expects: str) -> list[str]:
    return [n for n in ALL if load(n)["task"] == task and load(n)["expect"] in expects]


@pytest.mark.parametrize("name", ALL)
def test_fixture_files_are_well_formed(name: str) -> None:
    case = load(name)
    assert case["task"] in {"plan", "evaluate"}
    assert case["kind"] in {"http", "timeout"}
    assert case["expect"] in EXPECTATIONS
    assert case["description"]
    if case["kind"] == "http":
        assert 100 <= case["status"] <= 599


def test_required_failure_modes_are_covered() -> None:
    """Spec §11 'AI contract tests' requires each of these scenarios."""
    required = {
        "plan_valid.json",
        "plan_malformed_json.json",
        "plan_unsupported_operation.json",
        "plan_timeout.json",
        "plan_http_429.json",
        "plan_http_500.json",
        "plan_empty_content.json",
        "plan_hallucinated_bone.json",
    }
    assert required <= set(ALL)


@pytest.mark.parametrize("name", cases("plan", "accepted", "fallback"))
def test_plan_gate(
    name: str,
    selection: AnimationSelection,
    scope: SkeletalScope,
    scene_range: tuple[int, int],
) -> None:
    case = load(name)
    decision = accept_model_plan(
        content_of(case),
        model_id=case["body"].get("model", "unknown"),
        selection=selection,
        scope=scope,
        scene_range=scene_range,
    )
    if case["expect"] == "accepted":
        assert decision.accepted, decision.rejection_reasons
        assert decision.plan.plan_source is PlanSource.MODEL
    else:
        assert not decision.accepted
        assert decision.plan.plan_source is PlanSource.FALLBACK
        assert decision.rejection_reasons
        assert decision.plan.fallback_reason
    # Either way, the plan that comes out is always within scope and executable:
    assert set(decision.plan.target_bones) <= set(scope.chain_bones)


@pytest.mark.parametrize("name", cases("evaluate", "accepted", "degraded"))
def test_visual_judgement_validation(name: str) -> None:
    case = load(name)
    raw = content_of(case) or ""
    if case["expect"] == "accepted":
        ModelVisualJudgement.model_validate_json(raw)
    else:
        with pytest.raises(ValidationError):
            ModelVisualJudgement.model_validate_json(raw)


@pytest.mark.skip(reason="Phase 4: replay through httpx.MockTransport into TokenFactoryProvider")
@pytest.mark.parametrize(
    "name", cases("plan", "retryable_error", "fatal_error", "accepted_after_fence_strip")
)
def test_transport_classification(name: str) -> None:
    raise NotImplementedError
