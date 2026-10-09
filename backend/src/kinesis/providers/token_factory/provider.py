"""TokenFactoryProvider: planning on Nemotron, visual evaluation on a vision model (ADR 0009).

Contract (``ModelProvider``): transport failures raise ``ProviderError`` subclasses for the
orchestrator's retries and breakers; model-quality problems return ``INVALID`` with reasons,
never an exception. Plans always come back through ``accept_model_plan``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import httpx
from pydantic import ValidationError

from kinesis.errors import ProviderClientError, ProviderError
from kinesis.providers.base import (
    EvaluationRequest,
    PlanRequest,
    ProviderHealth,
    ProviderResult,
    ResultStatus,
    VisualAnalysisRequest,
    VisualFindings,
)
from kinesis.providers.token_factory.client import ChatResult, ModelInfo, TokenFactoryClient
from kinesis.providers.token_factory.prompts import (
    JUDGEMENT_SCHEMA,
    MAX_IMAGES_PER_REQUEST,
    PLAN_SCHEMA,
    evaluator_messages,
    image_part,
    planner_messages,
    response_format,
)
from kinesis.repair.plan_validation import accept_model_plan
from kinesis.schemas import ModelVisualJudgement, RepairPlan, TokenUsage, VisualEvaluation

log = logging.getLogger("kinesis.providers.token_factory")

PLANNER_MAX_TOKENS = 4096  # Super spends most of its budget on reasoning (spike S4)
EVALUATOR_MAX_TOKENS = 4096  # reasoning vision models (GLM, DeepSeek) think before answering


class TokenFactoryProvider:
    name = "token_factory"

    def __init__(
        self,
        client: TokenFactoryClient,
        *,
        planner_model: str,
        vision_model: str,
        classifier_model: str | None = None,
        prefer_nvidia_vision: bool = True,
    ) -> None:
        self.client = client
        self.prefer_nvidia_vision = prefer_nvidia_vision
        self.evaluator_max_tokens = EVALUATOR_MAX_TOKENS  # raise for reasoning vision models
        self.planner_model = planner_model
        self.vision_model = vision_model
        self.classifier_model = classifier_model
        self.prices: dict[str, tuple[float, float]] = {}

    @classmethod
    def from_settings(
        cls,
        *,
        api_key: str,
        base_url: str | None,
        planner_model: str | None,
        vision_model: str | None,
        classifier_model: str | None,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> TokenFactoryProvider:
        if not planner_model or not vision_model:
            raise ValueError("token_factory needs KINESIS_PLANNER_MODEL and KINESIS_VISION_MODEL")
        client = TokenFactoryClient(
            api_key,
            base_url=base_url or "https://api.tokenfactory.nebius.com/v1/",
            timeout_s=timeout_s,
            transport=transport,
        )
        return cls(
            client,
            planner_model=planner_model,
            vision_model=vision_model,
            classifier_model=classifier_model,
        )

    # ------------------------------------------------------------ cost

    def cost_usd(self, model: str, usage: TokenUsage) -> float | None:
        price = self.prices.get(model)
        if price is None:
            return None
        return (usage.prompt_tokens * price[0] + usage.completion_tokens * price[1]) / 1e6

    def _result[T](
        self, status: ResultStatus, value: T | None, chat: ChatResult, reasons: tuple[str, ...] = ()
    ) -> ProviderResult[T]:
        return ProviderResult(
            status=status,
            value=value,
            model_id=chat.model,
            usage=chat.usage,
            latency_ms=chat.latency_ms,
            reasons=reasons,
            cost_usd=self.cost_usd(chat.model, chat.usage),
        )

    # ------------------------------------------------------------ ModelProvider

    async def plan_repair(self, req: PlanRequest) -> ProviderResult[RepairPlan]:
        chat = await self.client.chat(
            self.planner_model,
            planner_messages(req),
            response_format=response_format("repair_plan", PLAN_SCHEMA),
            max_tokens=PLANNER_MAX_TOKENS,
            temperature=0.2,
        )
        decision = accept_model_plan(
            chat.content,
            model_id=chat.model,
            selection=req.selection,
            scope=req.scope,
            scene_range=req.scene_range,
        )
        reasons = decision.rejection_reasons
        if chat.finish_reason == "length" and not decision.accepted:
            reasons = (*reasons, "response truncated at max_tokens")
        status = ResultStatus.OK if decision.accepted else ResultStatus.INVALID
        return self._result(status, decision.plan, chat, reasons)

    async def evaluate_candidate(self, req: EvaluationRequest) -> ProviderResult[VisualEvaluation]:
        messages = await asyncio.to_thread(evaluator_messages, req)
        chat = await self.client.chat(
            self.vision_model,
            messages,
            response_format=response_format("visual_judgement", JUDGEMENT_SCHEMA),
            max_tokens=self.evaluator_max_tokens,
            temperature=0.0,
        )
        if chat.content is None:
            return self._result(ResultStatus.INVALID, None, chat, ("empty model response",))
        try:
            judgement = ModelVisualJudgement.model_validate_json(chat.content)
        except ValidationError as exc:
            reasons = tuple(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
            return self._result(ResultStatus.INVALID, None, chat, reasons)
        value = VisualEvaluation(
            candidate_id=req.candidate.candidate_id,
            contact_stability=judgement.contact_stability,
            naturalness=judgement.naturalness,
            artifacts_visible=judgement.artifacts_visible,
            performance_preservation=judgement.performance_preservation,
            instruction_adherence=judgement.instruction_adherence,
            notes=judgement.notes,
            prefers_over_original=judgement.prefers_over_original,
            model_id=chat.model,
            usage=chat.usage,
            latency_ms=chat.latency_ms,
        )
        return self._result(ResultStatus.OK, value, chat)

    async def analyze_visual(self, req: VisualAnalysisRequest) -> ProviderResult[VisualFindings]:
        frames: list[Path] = list(req.frames[:MAX_IMAGES_PER_REQUEST])
        content = [{"type": "text", "text": req.question[:1000]}]
        content += await asyncio.to_thread(lambda: [image_part(p) for p in frames])
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["answer", "confidence"],
            "properties": {
                "answer": {"type": "string", "maxLength": 1000},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        }
        chat = await self.client.chat(
            self.vision_model,
            [{"role": "user", "content": content}],
            response_format=response_format("visual_findings", schema),
            max_tokens=EVALUATOR_MAX_TOKENS,
            temperature=0.0,
        )
        try:
            data = json.loads(chat.content or "")
            value = VisualFindings(
                answer=str(data["answer"])[:1000],
                confidence=min(1.0, max(0.0, float(data["confidence"]))),
            )
        except (ValueError, KeyError, TypeError):
            return self._result(ResultStatus.INVALID, None, chat, ("unparseable findings",))
        return self._result(ResultStatus.OK, value, chat)

    # ------------------------------------------------------------ health / startup check

    async def verify_models(self) -> list[str]:
        """Check the catalog (ADR 0009 startup check) and load prices.

        Returns a list of problems; empty means the configuration is usable. Raises
        ``ProviderError`` only when the catalog itself cannot be read.
        """
        models = {m.id: m for m in await self.client.list_models(verbose=True)}
        for m in models.values():
            if m.price_in_per_m is not None and m.price_out_per_m is not None:
                self.prices[m.id] = (m.price_in_per_m, m.price_out_per_m)
        self._maybe_upgrade_vision(models)
        problems: list[str] = []
        planner: ModelInfo | None = models.get(self.planner_model)
        vision: ModelInfo | None = models.get(self.vision_model)
        if planner is None:
            problems.append(f"planner model {self.planner_model!r} is not in the catalog")
        elif not {"structured_outputs", "json_mode"} & planner.features:
            # Not fatal: Nemotron Super is tagged only tools/reasoning yet honours strict
            # json_schema (spike S4, live run 2026-10-08), and the plan gate validates anyway.
            log.warning(
                "provider.planner_structured_outputs_untagged",
                extra={"model": self.planner_model},
            )
        if vision is None:
            problems.append(f"vision model {self.vision_model!r} is not in the catalog")
        elif vision.input_modalities and not vision.accepts_images:
            problems.append(f"vision model {self.vision_model!r} does not accept images")
        if self.classifier_model and self.classifier_model not in models:
            problems.append(f"classifier model {self.classifier_model!r} is not in the catalog")
        return problems

    def _maybe_upgrade_vision(self, models: dict[str, ModelInfo]) -> None:
        """NVIDIA VL upgrade path (owner decision 2026-10-09): if the catalog ever lists an
        image-capable ``nvidia/*`` model, use it for visual evaluation (VL-named ids first)."""
        if not self.prefer_nvidia_vision or self.vision_model.lower().startswith("nvidia/"):
            return
        candidates = sorted(
            (
                m.id
                for m in models.values()
                if m.id.lower().startswith("nvidia/") and m.accepts_images
            ),
            key=lambda i: ("vl" not in i.lower(), i),
        )
        if candidates:
            log.warning(
                "provider.vision_model_upgraded",
                extra={"from": self.vision_model, "to": candidates[0]},
            )
            self.vision_model = candidates[0]

    async def health_check(self) -> ProviderHealth:
        try:
            problems = await self.verify_models()
        except ProviderError as exc:
            return ProviderHealth(ok=False, detail=f"catalog unreachable: {exc.message}")
        models = tuple(m for m in (self.planner_model, self.vision_model) if m)
        if problems:
            return ProviderHealth(ok=False, detail="; ".join(problems), models=models)
        return ProviderHealth(
            ok=True, detail="token factory reachable; models verified", models=models
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class ModelConfigError(ProviderClientError):
    """The configured models do not exist or lack a required capability (fail fast)."""


__all__ = ["ModelConfigError", "TokenFactoryProvider"]
