"""AI contract tests run against synthetic responses (tests/fixtures/ai_responses).

They check the validation gate (every model output that should not run becomes the
deterministic fallback plan, or a DEGRADED evaluation) and replay the same files through
``httpx.MockTransport`` into ``TokenFactoryProvider``, so transport classification, fence
stripping, usage parsing and request shapes are covered without the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from kinesis.errors import ProviderClientError, ProviderError, ProviderRateLimited, is_retryable
from kinesis.providers.base import EvaluationRequest, PlanRequest, ResultStatus
from kinesis.providers.token_factory import TokenFactoryProvider
from kinesis.repair.plan_validation import accept_model_plan
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    AnimationSelection,
    CandidateLabel,
    CandidateMetrics,
    DefectReport,
    ModelVisualJudgement,
    PlanSource,
    RepairCandidate,
    Severity,
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


# ------------------------------------------------------------------ transport replay (Phase 4)


def replay(case: dict[str, Any], seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if case["kind"] == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(case["status"], headers=case.get("headers") or {}, json=case["body"])

    return httpx.MockTransport(handler)


def provider_for(case: dict[str, Any], seen: list[httpx.Request]) -> TokenFactoryProvider:
    return TokenFactoryProvider.from_settings(
        api_key="test-key",
        base_url="https://tf.example/v1/",
        planner_model="nvidia/planner-test",
        vision_model="openbmb/vision-test",
        classifier_model=None,
        timeout_s=5,
        transport=replay(case, seen),
    )


def plan_request(selection: AnimationSelection, scope: SkeletalScope) -> PlanRequest:
    defect = DefectReport(intervals=(), severity=Severity.NONE, summary="", algorithm_version="v")
    return PlanRequest(selection, scope, defect, (1, 120))


@pytest.mark.parametrize("name", cases("plan", *EXPECTATIONS))
async def test_plan_transport_replay(
    name: str, selection: AnimationSelection, scope: SkeletalScope
) -> None:
    case = load(name)
    seen: list[httpx.Request] = []
    provider = provider_for(case, seen)
    expect = case["expect"]
    if expect in ("retryable_error", "fatal_error"):
        with pytest.raises(ProviderError) as err:
            await provider.plan_repair(plan_request(selection, scope))
        assert is_retryable(err.value) is (expect == "retryable_error")
        if case.get("status") == 429:
            assert isinstance(err.value, ProviderRateLimited)
            assert err.value.retry_after_s == 2.0
        if expect == "fatal_error":
            assert isinstance(err.value, ProviderClientError)  # neutral for the breaker
    else:
        result = await provider.plan_repair(plan_request(selection, scope))
        assert result.value is not None
        if expect in ("accepted", "accepted_after_fence_strip"):
            assert result.status is ResultStatus.OK, result.reasons
            assert result.value.plan_source is PlanSource.MODEL
        else:
            assert result.status is ResultStatus.INVALID
            assert result.value.plan_source is PlanSource.FALLBACK
            assert result.reasons
        if case["body"].get("usage"):
            assert result.usage.prompt_tokens == case["body"]["usage"]["prompt_tokens"]
    (request,) = seen
    assert request.headers["authorization"] == "Bearer test-key"
    assert str(request.url) == "https://tf.example/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "nvidia/planner-test"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "test-key" not in request.content.decode()
    await provider.aclose()


@pytest.mark.parametrize("name", cases("evaluate", "accepted", "degraded"))
async def test_evaluation_transport_replay(name: str, tmp_path: Path) -> None:
    case = load(name)
    seen: list[httpx.Request] = []
    provider = provider_for(case, seen)
    frame = tmp_path / "f.jpg"
    frame.write_bytes(bytes.fromhex("ffd8ffd9"))
    metrics = CandidateMetrics(
        planted_displacement_cm_before=10,
        planted_displacement_cm_after=1,
        slip_reduction_pct=90,
        contact_error_cm=0.1,
        penetration_max_cm=0,
        jerk_rms_ratio=1.2,
        joint_limit_violations=0,
        target_deviation_rms_cm=2,
        collateral_max_cm=0,
        outside_window_max_cm=0,
        root_deviation_cm=0,
        ik_unreachable_frames=0,
    )
    candidate = RepairCandidate(
        candidate_id="0123456789abcdef",
        label=CandidateLabel.A,
        parameters=DEFAULT_CANDIDATES[CandidateLabel.A],
    )
    selection = AnimationSelection.model_validate(
        {
            "scene_id": "scn_000001",
            "armature": "Rig",
            "target_bones": ["foot.L"],
            "temporal": {"frame_start": 45, "frame_end": 90},
        }
    )
    req = EvaluationRequest(
        selection, candidate, metrics, (frame,) * 5, (frame,) * 5, tuple(range(45, 50))
    )
    result = await provider.evaluate_candidate(req)
    if case["expect"] == "accepted":
        assert result.status is ResultStatus.OK
        assert result.value is not None
        assert result.value.candidate_id == "0123456789abcdef"
    else:
        assert result.status is ResultStatus.INVALID
        assert result.reasons
    body = json.loads(seen[0].content)
    images = [p for p in body["messages"][1]["content"] if p["type"] == "image_url"]
    assert len(images) == 10
    assert images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    await provider.aclose()
