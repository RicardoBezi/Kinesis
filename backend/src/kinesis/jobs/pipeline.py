"""The per-job repair DAG (ARCHITECTURE §3) and its event/aggregate bookkeeping.

Node results are plain dataclasses that flow along declared edges only. Every change to the
``RepairJob`` aggregate goes through ``JobContext.mutate`` (one lock per job), so concurrent
candidate branches cannot lose each other's updates.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from kinesis.analysis.detection import ContactPlane, Detection
from kinesis.analysis.extraction import ExtractedMotion, detect_from_extract, foot_and_toe
from kinesis.analysis.scope import resolve_temporal
from kinesis.errors import KinesisError, ProviderError, WorkerOutputInvalid
from kinesis.evaluation.metrics import (
    MetricContext,
    Samples,
    compute_metrics,
    jerk_rms,
    knee_flexion_deg,
    rank_candidates,
)
from kinesis.evaluation.recommend import recommend
from kinesis.jobs.specs import candidate_apply_spec, original_render_spec
from kinesis.jobs.worker_io import run_worker, spec_paths
from kinesis.observability.metrics import (
    PLAN_SOURCE,
    PROVIDER_CALLS,
    PROVIDER_COST,
    PROVIDER_LATENCY,
    PROVIDER_TOKENS,
    RENDER_DURATION,
    RETRIES,
    provider_error_outcome,
)
from kinesis.orchestration.cache import cache_key
from kinesis.orchestration.dag import JoinPolicy, Node, RetryPolicy
from kinesis.orchestration.engine import Codec, NodeEvent, NodeEventKind, SkipNode
from kinesis.orchestration.retry import call_with_retry
from kinesis.providers.base import EvaluationRequest, PlanRequest, ProviderResult, ResultStatus
from kinesis.repair.candidates import CandidateResult, LegChain, generate_candidate, repair_input
from kinesis.repair.plan_validation import default_plan
from kinesis.repair.render_plan import crop_render_spec
from kinesis.schemas import (
    ArtifactKind,
    ArtifactReference,
    CandidateLabel,
    CandidateMetrics,
    CandidateStatus,
    EvaluationReport,
    EvaluatorStatus,
    JobEvent,
    JobEventType,
    PlanMode,
    PlanSource,
    RepairCandidate,
    RepairJob,
    RepairPlan,
    SceneRef,
    VisualEvaluation,
    compute_candidate_id,
    compute_input_hash,
)
from kinesis.schemas.common import ErrorCode, ErrorInfo
from kinesis.schemas.worker import (
    WORKER_PROTOCOL_VERSION,
    ApplyRenderResult,
    ExtractResult,
    ExtractSpec,
    RenderSpec,
    WorkerCommand,
)

if TYPE_CHECKING:
    from kinesis.jobs.service import JobService

log = logging.getLogger("kinesis.pipeline")

ANALYSIS_NODES = (
    "validate_input",
    "extract_scope",
    "contact_analysis",
    "motion_analysis",
    "constraint_check",
    "defect_report",
    "plan_repair",
)
EVAL_FRAMES_PER_SIDE = 5  # spike S4: at most 10 images per vision request


def now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- node payloads


@dataclass(frozen=True)
class Analysis:
    extract: ExtractResult
    motion: ExtractedMotion
    detection: Detection
    features_bytes: bytes


@dataclass(frozen=True)
class Planned:
    analysis: Analysis
    plan: RepairPlan
    candidate_ids: dict[CandidateLabel, str]
    interval: tuple[int, int]


@dataclass(frozen=True)
class Generated:
    planned: Planned
    label: CandidateLabel
    result: CandidateResult


@dataclass(frozen=True)
class Applied:
    generated: Generated
    result: ApplyRenderResult


@dataclass(frozen=True)
class Scored:
    label: CandidateLabel
    candidate_id: str
    metrics: CandidateMetrics
    crop_frames: tuple[Path, ...]


@dataclass(frozen=True)
class OriginalRender:
    crop_frames: tuple[Path, ...]


# ---------------------------------------------------------------- context


@dataclass
class JobContext:
    service: JobService
    job: RepairJob
    scene: SceneRef
    job_dir: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    candidate_ids: dict[CandidateLabel, str] = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        return self.job.job_id

    async def mutate(self, fn: Callable[[RepairJob], RepairJob]) -> RepairJob:
        async with self.lock:
            self.job = fn(self.job)
            await self.service.store.save_job(self.job)
            return self.job

    async def set_candidate(self, label: CandidateLabel, **changes: Any) -> None:
        def apply(job: RepairJob) -> RepairJob:
            updated = tuple(
                c.model_copy(update=changes) if c.label is label else c for c in job.candidates
            )
            return job.model_copy(update={"candidates": updated})

        await self.mutate(apply)
        cid = self.candidate_ids.get(label)
        await self.emit(
            JobEventType.CANDIDATE_UPDATED,
            candidate_id=cid,
            message=f"candidate {label.value}: {changes.get('status', 'updated')}",
            data={"label": label.value, "status": str(changes.get("status", ""))},
        )

    async def emit(
        self,
        kind: JobEventType,
        *,
        node: str | None = None,
        candidate_id: str | None = None,
        attempt: int | None = None,
        message: str = "",
        data: Mapping[str, Any] | None = None,
    ) -> None:
        await self.service.store.append_event(
            JobEvent(
                seq=1,
                job_id=self.job_id,
                ts=now(),
                type=kind,
                node=node,
                candidate_id=candidate_id,
                attempt=attempt,
                message=message[:1000],
                data=dict(data or {}),
            )
        )

    async def register(
        self, relpath: str, kind: ArtifactKind, media_type: str, **kw: Any
    ) -> ArtifactReference:
        return await self.service.artifacts.register(self.job_id, relpath, kind, media_type, **kw)

    # ------------------------------------------------------------ DAG events

    def label_of(self, node: Node) -> CandidateLabel | None:
        tag = node.tags.get("candidate")
        return CandidateLabel(tag) if tag else None

    async def on_event(self, event: NodeEvent) -> None:
        node = event.node
        label = self.label_of(node)
        cid = self.candidate_ids.get(label) if label else None
        if event.kind is NodeEventKind.STARTED:
            await self.emit(
                JobEventType.NODE_STARTED, node=node.id, candidate_id=cid, attempt=event.attempt
            )
        elif event.kind in (NodeEventKind.SUCCEEDED, NodeEventKind.CACHE_HIT):
            await self.emit(
                JobEventType.NODE_SUCCEEDED,
                node=node.id,
                candidate_id=cid,
                attempt=event.attempt,
                data={"cached": event.kind is NodeEventKind.CACHE_HIT},
            )
        elif event.kind is NodeEventKind.SKIPPED:
            await self.emit(
                JobEventType.NODE_SKIPPED, node=node.id, candidate_id=cid, message=event.message
            )
        elif event.kind is NodeEventKind.RETRY:
            RETRIES.labels(node.id).inc()
            await self.emit(
                JobEventType.NODE_RETRY,
                node=node.id,
                candidate_id=cid,
                attempt=event.attempt,
                message=f"{node.id}: {describe(event.error)} (attempt {event.attempt}/"
                f"{node.retry.max_attempts}); retrying in {event.delay_s or 0:.1f}s",
            )
        elif event.kind is NodeEventKind.FAILED:
            info = error_info(event.error, node.id)
            if label is not None:
                other = "B" if label is CandidateLabel.A else "A"
                action = f"candidate {label.value} marked FAILED, candidate {other} unaffected"
            else:
                action = "the job cannot continue"
            message = (
                f"{node.id}: {describe(event.error)} (attempt {event.attempt}/"
                f"{node.retry.max_attempts}); {action}"
            )
            await self.emit(
                JobEventType.NODE_FAILED,
                node=node.id,
                candidate_id=cid,
                attempt=event.attempt,
                message=message,
                data={"code": info.code.value, "retryable": info.retryable},
            )
            if label is not None:
                await self.set_candidate(
                    label,
                    status=CandidateStatus.FAILED,
                    error=info.model_copy(update={"message": message[:2000]}),
                )


def describe(error: BaseException | None) -> str:
    if error is None:
        return "unknown error"
    text = str(error).split("\n", 1)[0][:300]
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def error_info(error: BaseException | None, node: str) -> ErrorInfo:
    if isinstance(error, KinesisError):
        return ErrorInfo(
            code=error.code, message=error.message[:2000], node=node, retryable=error.retryable
        )
    return ErrorInfo(code=ErrorCode.INTERNAL, message=describe(error)[:2000], node=node)


# ---------------------------------------------------------------- DAG


def build_repair_dag(ctx: JobContext) -> tuple[list[Node], dict[str, Codec]]:
    svc = ctx.service
    cfg = svc.config
    job = ctx.job
    selection = job.selection
    scope = job.skeletal_scope
    if scope is None:
        raise ValueError("job has no resolved skeletal scope")
    scene_range = (ctx.scene.frame_start, ctx.scene.frame_end)
    chain = LegChain(
        thigh=scope.keyable_bones[0],
        shin=scope.keyable_bones[1],
        foot=scope.keyable_bones[2],
        toe=next((b for b in scope.chain_bones if b not in scope.keyable_bones), None),
    )
    plane = ContactPlane(
        origin=selection.contact.plane_point, normal=selection.contact.plane_normal
    )

    async def validate_input(_: Mapping[str, Any]) -> tuple[int, int]:
        return resolve_temporal(selection.temporal, scene_range)

    async def extract_scope(inputs: Mapping[str, Any]) -> ExtractResult:
        start, end = inputs["validate_input"]
        spec = ExtractSpec(
            armature=scope.armature,
            chain_bones=scope.chain_bones,
            frame_start=start,
            frame_end=end,
            result_path=spec_paths("extract_scope")[1],
        )
        return await run_worker(
            svc.runner,
            ctx.job_dir,
            "extract_scope",
            WorkerCommand.EXTRACT,
            spec,
            ExtractResult,
            cfg.extract_timeout_s,
        )

    def extract_key(inputs: Mapping[str, Any]) -> str:
        return cache_key(
            "extract",
            ctx.scene.sha256,
            selection.model_dump(mode="json"),
            scope.model_dump(mode="json"),
            list(inputs["validate_input"]),
            WORKER_PROTOCOL_VERSION,
        )

    async def contact_analysis(inputs: Mapping[str, Any]) -> Analysis:
        extract: ExtractResult = inputs["extract_scope"]
        motion, detection = detect_from_extract(selection, scope, extract)
        features = detection.features.model_dump_json().encode()
        path = ctx.job_dir / "work" / "features.json"
        await asyncio.to_thread(path.write_bytes, features)
        await ctx.register("work/features.json", ArtifactKind.FEATURES_JSON, "application/json")
        return Analysis(extract, motion, detection, features)

    async def motion_analysis(inputs: Mapping[str, Any]) -> dict[str, float]:
        extract: ExtractResult = inputs["extract_scope"]
        ankle = np.array(next(c for c in extract.chain if c.name == chain.foot).world)[:, :3, 3]
        speed = np.linalg.norm(np.diff(ankle, axis=0), axis=1) * extract.fps
        return {
            "ankle_speed_max_m_s": float(speed.max(initial=0.0)),
            "ankle_jerk_rms": jerk_rms(ankle, extract.fps),
        }

    async def constraint_check(inputs: Mapping[str, Any]) -> dict[str, float]:
        extract: ExtractResult = inputs["extract_scope"]
        samples = Samples.from_worker(extract.scene_frame_start, extract.all_bones)
        flex = knee_flexion_deg(samples, chain.thigh, chain.shin)
        hip, ankle = samples.heads[chain.thigh], samples.heads[chain.foot]
        upper = np.linalg.norm(samples.heads[chain.shin] - hip, axis=1)
        lower = np.linalg.norm(ankle - samples.heads[chain.shin], axis=1)
        reach = np.linalg.norm(ankle - hip, axis=1) / (upper + lower)
        return {
            "original_knee_violations": float(np.sum((flex < 0) | (flex > 150))),
            "max_reach_ratio": float(reach.max()),
        }

    async def defect_report(inputs: Mapping[str, Any]) -> Analysis:
        analysis: Analysis = inputs["contact_analysis"]
        report = analysis.detection.report
        await ctx.mutate(lambda j: j.model_copy(update={"defect": report}))
        await ctx.emit(
            JobEventType.NODE_SUCCEEDED,
            node="defect_report",
            message=report.summary,
            data={
                "severity": report.severity.value,
                "motion": inputs["motion_analysis"],
                "constraints": inputs["constraint_check"],
            },
        )
        if report.worst is None or report.severity.value == "NONE":
            raise SkipNode("no defect detected")
        return analysis

    async def plan_repair(inputs: Mapping[str, Any]) -> Planned:
        analysis: Analysis = inputs["defect_report"]
        worst = analysis.detection.report.worst
        assert worst is not None  # defect_report skipped otherwise
        plan = await _plan(ctx, analysis, scene_range)
        interval = (worst.start, worst.end)
        input_hash = compute_input_hash(
            {
                "selection": selection.model_dump(mode="json"),
                "scope": scope.model_dump(mode="json"),
                "interval": list(interval),
            },
            analysis.features_bytes,
        )
        ids = {
            label: compute_candidate_id(input_hash, params)
            for label, params in plan.candidates.items()
        }
        ctx.candidate_ids.update(ids)
        PLAN_SOURCE.labels(plan.plan_source.value).inc()
        candidates = tuple(
            RepairCandidate(candidate_id=ids[label], label=label, parameters=plan.candidates[label])
            for label in sorted(plan.candidates)
        )
        await ctx.mutate(lambda j: j.model_copy(update={"plan": plan, "candidates": candidates}))
        return Planned(analysis, plan, ids, interval)

    def generate(label: CandidateLabel) -> Callable[[Mapping[str, Any]], Any]:
        async def fn(inputs: Mapping[str, Any]) -> Generated:
            planned: Planned = inputs["plan_repair"]
            await ctx.set_candidate(label, status=CandidateStatus.RUNNING)
            a = planned.analysis
            inp = repair_input(a.motion, chain, planned.interval, a.detection.floor_height, plane)
            result = generate_candidate(inp, planned.plan.candidates[label])
            if result.window is None:
                raise ValueError("the candidate produced no keys")
            return Generated(planned, label, result)

        return fn

    def render_spec(planned: Planned, blend_frames: int) -> RenderSpec:
        motion = planned.analysis.motion
        ankle, ball = foot_and_toe(motion, chain.foot, chain.toe)
        a, b = planned.interval
        return crop_render_spec(
            ankle,
            ball,
            motion.context_range[0],
            (a - blend_frames, b + blend_frames),
            motion.context_range,
        )

    def apply_render(label: CandidateLabel) -> Callable[[Mapping[str, Any]], Any]:
        node_id = f"apply_render_{label.value}"

        async def fn(inputs: Mapping[str, Any]) -> Applied:
            gen: Generated = inputs[f"generate_{label.value}"]
            planned = gen.planned
            cid = planned.candidate_ids[label]
            params = planned.plan.candidates[label]
            node, spec = candidate_apply_spec(
                ctx.job_id,
                label,
                cid,
                scope.armature,
                gen.result.bone_keys(),
                render_spec(planned, params.blend_frames) if cfg.render_previews else None,
            )
            result = await run_worker(
                svc.runner,
                ctx.job_dir,
                node,
                WorkerCommand.APPLY_RENDER,
                spec,
                ApplyRenderResult,
                cfg.apply_timeout_s,
            )
            if result.render_ms:
                RENDER_DURATION.labels(svc.runner.name).observe(result.render_ms / 1000)
            expected = planned.analysis.extract.original_action_hash
            if not (
                result.original_action_hash_before == result.original_action_hash_after == expected
            ):
                log.error(
                    "bug_alarm.original_action_modified",
                    extra={"job_id": ctx.job_id, "node": node_id},
                )
                raise WorkerOutputInvalid(
                    "original action hash changed during apply_render (non-destructive "
                    "guarantee violated)"
                )
            artifacts = [
                await ctx.register(
                    spec.output_blend, ArtifactKind.CANDIDATE_BLEND, "application/octet-stream"
                )
            ]
            start = planned.analysis.motion.context_range[0]
            if result.crop_frames:
                artifacts.append(
                    await ctx.register(
                        spec.frames_dir,
                        ArtifactKind.PREVIEW_FRAMES,
                        "image/jpeg",
                        frames=result.crop_frames,
                        first_frame=start,
                        frame_step=1,
                    )
                )
            if result.context_frames and spec.render is not None:
                artifacts.append(
                    await ctx.register(
                        spec.frames_dir,
                        ArtifactKind.CONTEXT_FRAMES,
                        "image/jpeg",
                        frames=result.context_frames,
                        first_frame=start,
                        frame_step=spec.render.context_every,
                    )
                )
            await ctx.set_candidate(label, artifacts=tuple(artifacts))
            return Applied(gen, result)

        return fn

    def metrics(label: CandidateLabel) -> Callable[[Mapping[str, Any]], Any]:
        async def fn(inputs: Mapping[str, Any]) -> Scored:
            applied: Applied = inputs[f"apply_render_{label.value}"]
            planned = applied.generated.planned
            a = planned.analysis
            params = planned.plan.candidates[label]
            m = compute_metrics(
                Samples.from_worker(a.extract.scene_frame_start, a.extract.all_bones),
                Samples.from_worker(a.extract.scene_frame_start, applied.result.all_bones),
                MetricContext(
                    fps=a.extract.fps,
                    thigh=chain.thigh,
                    shin=chain.shin,
                    foot=chain.foot,
                    toe=chain.toe,
                    chain_bones=frozenset(scope.chain_bones),
                    root_bones=scope.context_bones,
                    interval=planned.interval,
                    blend_frames=params.blend_frames,
                    context_range=a.motion.context_range,
                    floor_height=a.detection.floor_height,
                    plane=plane,
                ),
                applied.generated.result.ik_unreachable_frames,
            )
            cid = planned.candidate_ids[label]
            rel = f"candidates/{cid}/metrics.json"
            await asyncio.to_thread((ctx.job_dir / rel).write_text, m.model_dump_json(), "utf-8")
            ref = await ctx.register(rel, ArtifactKind.METRICS_JSON, "application/json")
            current = next(c for c in ctx.job.candidates if c.label is label)
            await ctx.set_candidate(
                label,
                status=CandidateStatus.SUCCEEDED,
                metrics=m,
                artifacts=(*current.artifacts, ref),
            )
            frames = tuple(ctx.job_dir / f for f in applied.result.crop_frames)
            return Scored(label, cid, m, frames)

        return fn

    async def render_original(inputs: Mapping[str, Any]) -> OriginalRender:
        planned: Planned = inputs["plan_repair"]
        if not cfg.render_previews:
            raise SkipNode("previews disabled")
        k = max(p.blend_frames for p in planned.plan.candidates.values())
        node, spec = original_render_spec(scope.armature, render_spec(planned, k))
        result = await run_worker(
            svc.runner,
            ctx.job_dir,
            node,
            WorkerCommand.APPLY_RENDER,
            spec,
            ApplyRenderResult,
            cfg.apply_timeout_s,
        )
        start = planned.analysis.motion.context_range[0]
        refs = []
        if result.crop_frames:
            refs.append(
                await ctx.register(
                    spec.frames_dir,
                    ArtifactKind.PREVIEW_FRAMES,
                    "image/jpeg",
                    frames=result.crop_frames,
                    first_frame=start,
                    frame_step=1,
                )
            )
        if result.context_frames and spec.render is not None:
            refs.append(
                await ctx.register(
                    spec.frames_dir,
                    ArtifactKind.CONTEXT_FRAMES,
                    "image/jpeg",
                    frames=result.context_frames,
                    first_frame=start,
                    frame_step=spec.render.context_every,
                )
            )
        await ctx.mutate(lambda j: j.model_copy(update={"original_artifacts": tuple(refs)}))
        return OriginalRender(tuple(ctx.job_dir / f for f in result.crop_frames))

    async def evaluate(inputs: Mapping[str, Any]) -> EvaluationReport:
        scored = [v for k, v in inputs.items() if k.startswith("metrics_")]
        if not scored:
            raise SkipNode("no candidate succeeded")
        original: OriginalRender | None = inputs.get("render_original")
        report = await _evaluate(ctx, scored, original)
        await ctx.mutate(lambda j: j.model_copy(update={"evaluation": report}))
        return report

    worker_retry = RetryPolicy(max_attempts=3)  # extract: 2 retries
    apply_retry = RetryPolicy(max_attempts=2)  # apply_render: 1 retry
    nodes: list[Node] = [
        Node("validate_input", validate_input, timeout_s=5),
        Node(
            "extract_scope",
            extract_scope,
            ("validate_input",),
            retry=worker_retry,
            timeout_s=cfg.extract_timeout_s + 30,
            cache_key=extract_key,
        ),
        Node("contact_analysis", contact_analysis, ("extract_scope",), timeout_s=30),
        Node("motion_analysis", motion_analysis, ("extract_scope",), timeout_s=30),
        Node("constraint_check", constraint_check, ("extract_scope",), timeout_s=30),
        Node(
            "defect_report",
            defect_report,
            ("contact_analysis", "motion_analysis", "constraint_check"),
            timeout_s=10,
        ),
        Node("plan_repair", plan_repair, ("defect_report",), timeout_s=cfg.plan_timeout_s),
        Node(
            "render_original",
            render_original,
            ("plan_repair",),
            retry=apply_retry,
            timeout_s=cfg.apply_timeout_s + 30,
        ),
    ]
    for label in (CandidateLabel.A, CandidateLabel.B):
        tags = {"candidate": label.value}
        nodes += [
            Node(
                f"generate_{label.value}",
                generate(label),
                ("plan_repair",),
                timeout_s=30,
                tags=tags,
            ),
            Node(
                f"apply_render_{label.value}",
                apply_render(label),
                (f"generate_{label.value}",),
                retry=apply_retry,
                timeout_s=cfg.apply_timeout_s + 30,
                tags=tags,
            ),
            Node(
                f"metrics_{label.value}",
                metrics(label),
                (f"apply_render_{label.value}",),
                timeout_s=30,
                tags=tags,
            ),
        ]
    nodes.append(
        Node(
            "evaluate",
            evaluate,
            ("metrics_A", "metrics_B", "render_original"),
            join=JoinPolicy.ANY_SUCCESS,
            timeout_s=cfg.evaluate_timeout_s,
        )
    )
    codecs = {
        "extract_scope": Codec(
            encode=lambda v: v.model_dump_json(), decode=ExtractResult.model_validate_json
        )
    }
    return nodes, codecs


# ---------------------------------------------------------------- provider steps


async def _plan(ctx: JobContext, analysis: Analysis, scene_range: tuple[int, int]) -> RepairPlan:
    svc = ctx.service
    job = ctx.job
    scope = job.skeletal_scope
    assert scope is not None
    if job.plan_mode is PlanMode.DETERMINISTIC:
        return default_plan(job.selection, scope, "plan_mode=DETERMINISTIC")
    req = PlanRequest(job.selection, scope, analysis.detection.report, scene_range)

    async def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
        RETRIES.labels("plan_repair").inc()
        await ctx.emit(
            JobEventType.NODE_RETRY,
            node="plan_repair",
            attempt=attempt,
            message=f"plan_repair: {describe(exc)} (attempt {attempt}/"
            f"{svc.config.provider_retry.max_attempts}); retrying in {delay:.1f}s",
        )

    try:
        result = await call_with_retry(
            lambda: svc.planner_breaker.call(lambda: svc.provider.plan_repair(req)),
            svc.config.provider_retry,
            sleep=svc.sleep,
            jitter=svc.jitter,
            on_retry=on_retry,
        )
    except ProviderError as exc:
        reason = f"{exc.code.value}: {describe(exc)}"
        model = getattr(svc.provider, "planner_model", svc.provider.name)
        PROVIDER_CALLS.labels(model, "plan", provider_error_outcome(exc)).inc()
        await ctx.emit(
            JobEventType.PLAN_REJECTED,
            node="plan_repair",
            message=f"fallback plan used ({reason})",
            data={"code": exc.code.value},
        )
        return default_plan(job.selection, scope, reason)
    record_model_call("plan", result)
    plan = result.value or default_plan(job.selection, scope, "provider returned no plan")
    if result.status is ResultStatus.INVALID:
        await ctx.emit(
            JobEventType.PLAN_REJECTED,
            node="plan_repair",
            message="model plan failed validation; fallback plan used",
            data={"code": ErrorCode.PLAN_INVALID.value, "reasons": list(result.reasons)[:10]},
        )
    if plan.plan_source is PlanSource.MODEL and result.status is not ResultStatus.OK:
        plan = default_plan(job.selection, scope, "; ".join(result.reasons) or "invalid plan")
    return plan


async def _evaluate_one(
    ctx: JobContext, s: Scored, original: OriginalRender
) -> VisualEvaluation | None:
    """One candidate's visual judgement; ``None`` (DEGRADED) on any provider problem."""
    svc = ctx.service
    candidate = next(c for c in ctx.job.candidates if c.label is s.label)
    idx = [i for i in _sample(s.crop_frames, EVAL_FRAMES_PER_SIDE) if i < len(original.crop_frames)]
    first = ctx.job.selection.temporal.clipped(ctx.scene.frame_start, ctx.scene.frame_end)[0]
    req = EvaluationRequest(
        selection=ctx.job.selection,
        candidate=candidate,
        metrics=s.metrics,
        original_frames=tuple(original.crop_frames[i] for i in idx),
        candidate_frames=tuple(s.crop_frames[i] for i in idx),
        frame_numbers=tuple(first + i for i in idx),
    )
    try:
        result = await call_with_retry(
            lambda: svc.vision_breaker.call(lambda: svc.provider.evaluate_candidate(req)),
            svc.config.provider_retry,
            sleep=svc.sleep,
            jitter=svc.jitter,
        )
    except ProviderError as exc:
        model = getattr(svc.provider, "vision_model", svc.provider.name)
        PROVIDER_CALLS.labels(model, "evaluate", provider_error_outcome(exc)).inc()
        log.warning("evaluate.provider_failed", extra={"error": describe(exc)})
        return None
    record_model_call("evaluate", result)
    if not result.ok:
        log.warning("evaluate.invalid_judgement", extra={"reasons": list(result.reasons)[:5]})
    return result.value if result.ok else None


def record_model_call(task: str, result: ProviderResult[Any]) -> None:
    """Prometheus counters and one structured log line per model call (token and cost)."""
    model = result.model_id or "unknown"
    outcome = "ok" if result.status is ResultStatus.OK else "invalid"
    PROVIDER_CALLS.labels(model, task, outcome).inc()
    PROVIDER_LATENCY.labels(model, task).observe(result.latency_ms / 1000)
    PROVIDER_TOKENS.labels(model, "prompt").inc(result.usage.prompt_tokens)
    PROVIDER_TOKENS.labels(model, "completion").inc(result.usage.completion_tokens)
    if result.cost_usd is not None:
        PROVIDER_COST.labels(model).inc(result.cost_usd)
    log.info(
        "model.call",
        extra={
            "task": task,
            "model": model,
            "status": result.status.value,
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "cost_estimate_usd": result.cost_usd,
            "latency_ms": result.latency_ms,
        },
    )


def _sample(paths: Sequence[Path], count: int) -> tuple[int, ...]:
    if not paths:
        return ()
    idx = np.linspace(0, len(paths) - 1, min(count, len(paths)))
    return tuple(sorted({round(float(i)) for i in idx}))


async def _evaluate(
    ctx: JobContext, scored: list[Scored], original: OriginalRender | None
) -> EvaluationReport:
    svc = ctx.service
    ranking = rank_candidates([(s.label, s.candidate_id, s.metrics) for s in scored])
    visual: list[VisualEvaluation] = []
    if svc.provider.name == "null":
        evaluator_status = EvaluatorStatus.SKIPPED
    elif original is None or not original.crop_frames:
        evaluator_status = EvaluatorStatus.DEGRADED  # nothing to compare against
    else:
        for s in scored:
            value = await _evaluate_one(ctx, s, original)
            if value is not None:
                visual.append(value)
        ok = len(visual) == len(scored)
        evaluator_status = EvaluatorStatus.OK if ok else EvaluatorStatus.DEGRADED
    rec = recommend(
        ranking,
        {v.candidate_id: v for v in visual},
        visual_ok=evaluator_status is EvaluatorStatus.OK,
    )
    reason = rec.reason
    if evaluator_status is EvaluatorStatus.DEGRADED:
        reason += (
            " Visual evaluation was unavailable for some candidates; ranking is objective only."
        )
    return EvaluationReport(
        objective_ranking=ranking,
        visual=tuple(visual),
        recommended=rec.label,
        recommendation_reason=reason[:2000],
        evaluator_status=evaluator_status,
        scoring_weights=rec.weights,
    )


__all__ = ["ANALYSIS_NODES", "JobContext", "build_repair_dag", "describe", "error_info"]
