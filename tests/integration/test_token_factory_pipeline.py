"""A whole job with TokenFactoryProvider talking to a mocked Token Factory (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from conftest import run_job
from prometheus_client import REGISTRY

from kinesis.providers.token_factory import TokenFactoryProvider
from kinesis.schemas import EvaluatorStatus, JobStatus, PlanSource

PLANNER = "nvidia/nemotron-test-planner"
VISION = "openbmb/vision-test"


class FakeTokenFactory:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": PLANNER,
                            "architecture": {"modality": "text->text"},
                            "supported_features": ["structured_outputs"],
                            "pricing": {"prompt": 0.3, "completion": 0.9},
                        },
                        {
                            "id": VISION,
                            "architecture": {"modality": "text+image->text"},
                            "pricing": {"prompt": 0.66, "completion": 1.11},
                        },
                    ]
                },
            )
        body = json.loads(request.content)
        self.requests.append(body)
        if body["model"] == PLANNER:
            context = json.loads(
                body["messages"][1]["content"].split("```json\n")[1].split("```")[0]
            )
            sel = context["selection"]
            plan = {
                "repair_type": "FOOT_CONTACT",
                **{k: sel[k] for k in ("target_bones", "frame_start", "frame_end")},
                "context_frames_before": sel["context_frames_before"],
                "context_frames_after": sel["context_frames_after"],
                "candidates": {
                    "A": {
                        "lock_strength": 1.0,
                        "anchor_mode": "ONSET",
                        "blend_frames": 4,
                        "tolerance_cm": 0.2,
                        "lock_yaw": True,
                        "smoothing_window": 0,
                        "height_clamp": True,
                    },
                    "B": {
                        "lock_strength": 0.8,
                        "anchor_mode": "MEAN",
                        "blend_frames": 10,
                        "tolerance_cm": 1.0,
                        "lock_yaw": False,
                        "smoothing_window": 5,
                        "height_clamp": True,
                    },
                },
                "explanation": "A locks hard; B keeps more of the original drift.",
            }
            content = f"```json\n{json.dumps(plan)}\n```"
            usage = {"prompt_tokens": 900, "completion_tokens": 700}
        else:
            content = json.dumps(
                {
                    "contact_stability": 5,
                    "naturalness": 4,
                    "artifacts_visible": 5,
                    "performance_preservation": 4,
                    "instruction_adherence": 3,
                    "notes": "foot holds",
                    "prefers_over_original": True,
                }
            )
            usage = {"prompt_tokens": 3000, "completion_tokens": 60}
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": usage,
            },
        )


def _metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_job_uses_model_plan_and_visual_evaluation(make_service: Any) -> None:
    fake = FakeTokenFactory()
    provider = TokenFactoryProvider.from_settings(
        api_key="k",
        base_url="https://tf.example/v1",
        planner_model=PLANNER,
        vision_model=VISION,
        classifier_model=None,
        timeout_s=5,
        transport=httpx.MockTransport(fake),
    )
    assert await provider.verify_models() == []
    cost_before = _metric("kinesis_model_cost_usd_total", model=PLANNER)
    service = make_service(provider=provider)
    job = await run_job(service, instruction="keep the heel planted")

    assert job.status is JobStatus.AWAITING_DECISION
    assert job.plan is not None
    assert job.plan.plan_source is PlanSource.MODEL
    assert job.plan.model_id == PLANNER
    assert job.plan.candidates["A"].blend_frames == 4  # the model's parameters were used
    assert job.evaluation is not None
    assert job.evaluation.evaluator_status is EvaluatorStatus.OK
    assert {v.model_id for v in job.evaluation.visual} == {VISION}
    assert all(v.usage.prompt_tokens == 3000 for v in job.evaluation.visual)

    vision_calls = [r for r in fake.requests if r["model"] == VISION]
    assert len(vision_calls) == 2
    for call in vision_calls:
        images = [p for p in call["messages"][1]["content"] if p["type"] == "image_url"]
        assert 2 <= len(images) <= 10
    planner_call = next(r for r in fake.requests if r["model"] == PLANNER)
    assert "keep the heel planted" in planner_call["messages"][1]["content"]
    expected = (900 * 0.3 + 700 * 0.9) / 1e6
    assert _metric("kinesis_model_cost_usd_total", model=PLANNER) - cost_before == pytest.approx(
        expected
    )
    await provider.aclose()
