"""JobService: everything the API does with scenes and jobs (API.md, FAILURE_MODES).

- uploads are streamed with a size cap and a ``BLENDER`` magic check, then inspected
  headlessly;
- job requests are validated against the scene inventory before anything is stored;
- each job runs its repair DAG in a background task; the decision runs a one-node DAG
  (``apply_selected``) that exports the chosen layer;
- jobs left RUNNING or APPLYING by a previous process are marked FAILED at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import random
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kinesis.analysis.scope import resolve_skeletal, resolve_temporal
from kinesis.errors import KinesisError, SelectionInvalid
from kinesis.jobs.pipeline import ANALYSIS_NODES, JobContext, build_repair_dag, error_info, now
from kinesis.jobs.runner import JobRunner
from kinesis.jobs.specs import export_spec
from kinesis.jobs.worker_io import run_worker, spec_paths
from kinesis.observability.context import bind
from kinesis.observability.metrics import JOBS_FINISHED, NODE_SECONDS
from kinesis.orchestration.breaker import CircuitBreaker
from kinesis.orchestration.cache import Cache
from kinesis.orchestration.dag import Node, NodeStatus, RetryPolicy
from kinesis.orchestration.engine import DagResult, DagRunner
from kinesis.providers.base import ModelProvider
from kinesis.repair.plan_validation import SUPPORTED_REPAIR_TYPES
from kinesis.schemas import (
    ArtifactKind,
    CandidateMetrics,
    CandidateStatus,
    CreateRepairJobRequest,
    DecisionChoice,
    DecisionRequest,
    DefectReport,
    EvaluationReport,
    EventPage,
    HumanDecision,
    JobStatus,
    RepairCandidate,
    RepairJob,
    SceneRef,
)
from kinesis.schemas.candidate import canonical_json
from kinesis.schemas.common import CandidateLabel, ErrorCode, ErrorInfo
from kinesis.schemas.worker import ExportResult, InspectResult, InspectSpec, WorkerCommand
from kinesis.storage.ids import new_id
from kinesis.storage.sqlite import SqliteArtifactStore, SqliteJobStore
from kinesis.storage.store import IdempotencyConflict, InvalidTransition

log = logging.getLogger("kinesis.jobs")

BLEND_MAGIC = b"BLENDER"


class RequestError(Exception):
    """A request the API answers with a Problem: ``status`` + machine-readable ``code``."""

    def __init__(self, status: int, code: ErrorCode, title: str, detail: str | None = None):
        super().__init__(title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


def not_found(code: ErrorCode, what: str) -> RequestError:
    return RequestError(404, code, f"{what} not found")


@dataclass(frozen=True)
class ServiceConfig:
    data_dir: Path
    max_upload_bytes: int = 200 * 1024 * 1024
    inspect_timeout_s: float = 120
    extract_timeout_s: float = 120
    apply_timeout_s: float = 300
    export_timeout_s: float = 120
    plan_timeout_s: float = 90
    evaluate_timeout_s: float = 180
    render_previews: bool = True
    provider_retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_attempts=3))

    @property
    def scenes_root(self) -> Path:
        return self.data_dir / "scenes"


class JobService:
    def __init__(
        self,
        store: SqliteJobStore,
        artifacts: SqliteArtifactStore,
        runner: JobRunner,
        provider: ModelProvider,
        config: ServiceConfig,
        *,
        cache: Cache | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.runner = runner
        self.provider = provider
        self.config = config
        self.cache = cache
        self.sleep = sleep
        self.jitter = jitter
        self.planner_breaker = CircuitBreaker(f"{provider.name}:planner")
        self.vision_breaker = CircuitBreaker(f"{provider.name}:vision")
        self._tasks: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------ scenes

    def scene_dir(self, scene_id: str) -> Path:
        root = self.config.scenes_root.resolve()
        path = (root / scene_id).resolve()
        if not path.is_relative_to(root) or path == root:
            raise not_found(ErrorCode.SCENE_NOT_FOUND, "scene")
        return path

    async def upload_scene(self, chunks: AsyncIterator[bytes]) -> SceneRef:
        scene_id = new_id("scn")
        scene_dir = self.scene_dir(scene_id)
        target = scene_dir / "input" / "scene.blend"
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        head = b""
        try:
            with target.open("wb") as fh:
                async for chunk in chunks:
                    if len(head) < len(BLEND_MAGIC):
                        head += chunk[: len(BLEND_MAGIC) - len(head)]
                        if len(head) >= len(BLEND_MAGIC) and head != BLEND_MAGIC:
                            raise RequestError(
                                422,
                                ErrorCode.FILE_INVALID,
                                "Not a .blend file",
                                "the file does not start with the BLENDER magic bytes",
                            )
                    size += len(chunk)
                    if size > self.config.max_upload_bytes:
                        raise RequestError(
                            413,
                            ErrorCode.FILE_TOO_LARGE,
                            "File too large",
                            f"the limit is {self.config.max_upload_bytes} bytes",
                        )
                    digest.update(chunk)
                    fh.write(chunk)
            if head != BLEND_MAGIC:
                raise RequestError(
                    422, ErrorCode.FILE_INVALID, "Not a .blend file", "empty or truncated"
                )
            spec = InspectSpec(result_path=spec_paths("inspect")[1])
            try:
                info = await run_worker(
                    self.runner,
                    scene_dir,
                    "inspect",
                    WorkerCommand.INSPECT,
                    spec,
                    InspectResult,
                    self.config.inspect_timeout_s,
                )
            except KinesisError as exc:
                raise RequestError(
                    422, ErrorCode.FILE_INVALID, "Scene could not be inspected", exc.message[:500]
                ) from exc
        except BaseException:
            shutil.rmtree(scene_dir, ignore_errors=True)
            raise
        scene = SceneRef(
            scene_id=scene_id,
            sha256=digest.hexdigest(),
            size_bytes=size,
            frame_start=info.frame_start,
            frame_end=info.frame_end,
            fps=info.fps,
            blender_version=info.blender_version[:32],
            armatures=info.armatures,
        )
        await self.store.put_scene(scene)
        return scene

    async def get_scene(self, scene_id: str) -> SceneRef:
        scene = await self.store.get_scene(scene_id)
        if scene is None:
            raise not_found(ErrorCode.SCENE_NOT_FOUND, "scene")
        return scene

    # ------------------------------------------------------------ jobs

    async def create_job(
        self, body: CreateRepairJobRequest, idempotency_key: str
    ) -> tuple[RepairJob, bool]:
        body_hash = hashlib.sha256(
            canonical_json(body.model_dump(mode="json")).encode()
        ).hexdigest()
        sel = body.selection
        scene = await self.get_scene(sel.scene_id)
        if body.repair_type not in SUPPORTED_REPAIR_TYPES:
            raise RequestError(422, ErrorCode.UNSUPPORTED_REPAIR_TYPE, "Unsupported repair type")
        armature = next((a for a in scene.armatures if a.name == sel.armature), None)
        if armature is None:
            raise RequestError(
                422,
                ErrorCode.ARMATURE_NOT_FOUND,
                "Armature not found",
                f"{sel.armature!r} is not in the scene",
            )
        parents = {b.name: b.parent for b in armature.bones}
        try:
            scope = resolve_skeletal(sel.armature, sel.target_bones, parents)
            resolve_temporal(sel.temporal, (scene.frame_start, scene.frame_end))
        except SelectionInvalid as exc:
            raise RequestError(422, exc.code, "Invalid selection", exc.message) from exc
        stamp = now()
        job = RepairJob(
            job_id=new_id("job"),
            idempotency_key=idempotency_key,
            repair_type=body.repair_type,
            plan_mode=body.plan_mode,
            selection=sel,
            status=JobStatus.PENDING,
            created_at=stamp,
            updated_at=stamp,
            skeletal_scope=scope,
        )
        try:
            stored, created = await self.store.create_job(
                job, idempotency_key=idempotency_key, body_hash=body_hash
            )
        except IdempotencyConflict as exc:
            raise RequestError(
                409, ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency-Key reused with a different body"
            ) from exc
        if created:
            job_dir = self.artifacts.job_dir(stored.job_id)
            (job_dir / "input").mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                shutil.copyfile,
                self.scene_dir(scene.scene_id) / "input" / "scene.blend",
                job_dir / "input" / "scene.blend",
            )
            self._spawn(stored.job_id, self._run(stored.job_id, scene))
        return stored, created

    def _spawn(self, job_id: str, coro: Awaitable[None]) -> None:
        task: asyncio.Task[None] = asyncio.ensure_future(coro)
        self._tasks[job_id] = task

        def forget(done: asyncio.Task[None]) -> None:
            if self._tasks.get(job_id) is done:
                del self._tasks[job_id]

        task.add_done_callback(forget)

    async def wait_idle(self) -> None:
        """Wait for every background job task (tests and graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)

    async def _transition(self, job_id: str, to: JobStatus, message: str) -> RepairJob:
        job = await self.store.transition(job_id, to, message=message)
        if to is not JobStatus.RUNNING and to is not JobStatus.APPLYING:
            JOBS_FINISHED.labels(to.value).inc()
        return job

    @staticmethod
    def _observe(result: DagResult) -> None:
        for node_id, outcome in result.outcomes.items():
            if outcome.status not in (NodeStatus.PENDING, NodeStatus.SKIPPED):
                NODE_SECONDS.labels(node_id, outcome.status.value).observe(
                    outcome.elapsed_ms / 1000
                )

    async def _run(self, job_id: str, scene: SceneRef) -> None:
        with bind(job_id=job_id):
            await self._run_bound(job_id, scene)

    async def _run_bound(self, job_id: str, scene: SceneRef) -> None:
        job = await self.store.transition(job_id, JobStatus.RUNNING, message="repair DAG started")
        ctx = JobContext(self, job, scene, self.artifacts.job_dir(job_id))
        try:
            nodes, codecs = build_repair_dag(ctx)
            runner = DagRunner(
                on_event=ctx.on_event,
                cache=self.cache,
                codecs=codecs,
                sleep=self.sleep,
                jitter=self.jitter,
            )
            result = await runner.run(nodes)
            self._observe(result)
            await self._finalize(ctx, result)
        except asyncio.CancelledError:
            raise  # cancel() owns the CANCELLED transition
        except Exception as exc:
            log.exception("job.crashed", extra={"job_id": job_id})
            await self._fail(ctx, ErrorInfo(code=ErrorCode.INTERNAL, message=str(exc)[:2000]))

    async def _fail(self, ctx: JobContext, error: ErrorInfo) -> None:
        await ctx.mutate(lambda j: j.model_copy(update={"error": error}))
        with contextlib.suppress(InvalidTransition):
            await self._transition(ctx.job_id, JobStatus.FAILED, error.message)

    async def _finalize(self, ctx: JobContext, result: DagResult) -> None:
        for node_id in ANALYSIS_NODES:
            outcome = result.outcomes[node_id]
            if outcome.status is NodeStatus.FAILED:
                await self._fail(ctx, error_info(outcome.error, node_id))
                return
        if result.outcomes["defect_report"].status is NodeStatus.SKIPPED:
            # defect_report only skips itself (SkipNode) when there is nothing to repair
            await self._transition(
                ctx.job_id, JobStatus.COMPLETED, "no defect detected; nothing to repair"
            )
            return
        if any(c.status is CandidateStatus.SUCCEEDED for c in ctx.job.candidates):
            await self._transition(
                ctx.job_id, JobStatus.AWAITING_DECISION, "candidates ready for review"
            )
            return
        await self._fail(
            ctx,
            ErrorInfo(
                code=ErrorCode.NO_VIABLE_CANDIDATE,
                message="every repair candidate failed; see the candidates' errors",
                node="evaluate",
            ),
        )

    # ------------------------------------------------------------ decision

    async def decide(self, job_id: str, body: DecisionRequest) -> RepairJob:
        job = await self.get_job(job_id)
        if job.status is not JobStatus.AWAITING_DECISION:
            raise RequestError(
                409,
                ErrorCode.INVALID_STATE,
                "Job is not awaiting a decision",
                f"status {job.status}",
            )
        candidate: RepairCandidate | None = None
        if body.choice is not DecisionChoice.REJECT_ALL:
            candidate = next(
                (c for c in job.candidates if c.label.value == body.choice.value), None
            )
            if candidate is None or candidate.status is not CandidateStatus.SUCCEEDED:
                raise RequestError(
                    422,
                    ErrorCode.VALIDATION_ERROR,
                    "Candidate cannot be chosen",
                    f"candidate {body.choice.value} did not succeed",
                )
        decision = HumanDecision(
            job_id=job_id,
            choice=body.choice,
            candidate_id=candidate.candidate_id if candidate else None,
            model_recommended=job.evaluation.recommended if job.evaluation else None,
            decided_at=now(),
            time_to_decision_s=body.time_to_decision_s,
            note=body.note,
        )
        await self.store.record_decision(decision)
        await self.store.save_job(job.model_copy(update={"decision": decision}))
        if candidate is None:
            return await self._transition(job_id, JobStatus.REJECTED, "all candidates rejected")
        applying = await self.store.transition(
            job_id, JobStatus.APPLYING, message=f"applying candidate {candidate.label.value}"
        )
        scene = await self.get_scene(job.selection.scene_id)
        self._spawn(job_id, self._apply(applying, scene, candidate))
        return applying

    async def _apply(self, job: RepairJob, scene: SceneRef, candidate: RepairCandidate) -> None:
        with bind(job_id=job.job_id):
            await self._apply_bound(job, scene, candidate)

    async def _apply_bound(
        self, job: RepairJob, scene: SceneRef, candidate: RepairCandidate
    ) -> None:
        ctx = JobContext(self, job, scene, self.artifacts.job_dir(job.job_id))
        label: CandidateLabel = candidate.label

        async def apply_selected(_: Mapping[str, Any]) -> ExportResult:
            node, spec = export_spec("scene", job.selection.armature, label, candidate.candidate_id)
            result = await run_worker(
                self.runner,
                ctx.job_dir,
                node,
                WorkerCommand.EXPORT,
                spec,
                ExportResult,
                self.config.export_timeout_s,
            )
            ref = await ctx.register(
                result.output_blend, ArtifactKind.OUTPUT_BLEND, "application/octet-stream"
            )
            await ctx.mutate(lambda j: j.model_copy(update={"output": ref}))
            return result

        node = Node(
            "apply_selected",
            apply_selected,
            retry=RetryPolicy(max_attempts=2),
            timeout_s=self.config.export_timeout_s + 30,
        )
        try:
            result = await DagRunner(
                on_event=ctx.on_event, sleep=self.sleep, jitter=self.jitter
            ).run([node])
        except asyncio.CancelledError:
            raise
        outcome = result.outcomes["apply_selected"]
        if outcome.status is NodeStatus.SUCCEEDED:
            await self._transition(job.job_id, JobStatus.COMPLETED, "repair applied")
        else:
            await self._fail(ctx, error_info(outcome.error, "apply_selected"))

    # ------------------------------------------------------------ cancel / recovery

    async def cancel(self, job_id: str) -> RepairJob:
        job = await self.get_job(job_id)
        if job.status.is_terminal or job.status is JobStatus.APPLYING:
            raise RequestError(
                409, ErrorCode.INVALID_STATE, "Job cannot be cancelled", f"status {job.status}"
            )
        task = self._tasks.pop(job_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            return await self._transition(job_id, JobStatus.CANCELLED, "cancelled by user")
        except InvalidTransition as exc:
            current = await self.get_job(job_id)
            raise RequestError(
                409, ErrorCode.INVALID_STATE, "Job cannot be cancelled", f"status {current.status}"
            ) from exc

    async def recover(self) -> int:
        """FAILURE_MODES #26: jobs interrupted by a restart are marked FAILED."""
        count = 0
        for status in (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.APPLYING):
            for job_id in await self.store.list_jobs_with_status(status):
                job = await self.store.get_job(job_id)
                if job is None:
                    continue
                error = ErrorInfo(
                    code=ErrorCode.INTERNAL, message="interrupted by a server restart"
                )
                await self.store.save_job(job.model_copy(update={"error": error}))
                await self._transition(job_id, JobStatus.FAILED, error.message)
                count += 1
        return count

    # ------------------------------------------------------------ reads

    async def get_job(self, job_id: str) -> RepairJob:
        job = await self.store.get_job(job_id)
        if job is None:
            raise not_found(ErrorCode.JOB_NOT_FOUND, "job")
        return job

    async def events(self, job_id: str, after_seq: int, limit: int) -> EventPage:
        await self.get_job(job_id)
        events = tuple(await self.store.list_events(job_id, after_seq=after_seq, limit=limit))
        return EventPage(events=events, next_seq=events[-1].seq if events else after_seq)

    async def defect(self, job_id: str) -> DefectReport:
        job = await self.get_job(job_id)
        if job.defect is None:
            raise RequestError(409, ErrorCode.NOT_READY, "Defect report not ready")
        return job.defect

    async def candidates(self, job_id: str) -> list[RepairCandidate]:
        return list((await self.get_job(job_id)).candidates)

    async def metrics(self, job_id: str, candidate_id: str) -> CandidateMetrics:
        job = await self.get_job(job_id)
        candidate = next((c for c in job.candidates if c.candidate_id == candidate_id), None)
        if candidate is None:
            raise not_found(ErrorCode.CANDIDATE_NOT_FOUND, "candidate")
        if candidate.metrics is None:
            raise RequestError(409, ErrorCode.NOT_READY, "Metrics not ready")
        return candidate.metrics

    async def evaluation(self, job_id: str) -> EvaluationReport:
        job = await self.get_job(job_id)
        if job.evaluation is None:
            raise RequestError(409, ErrorCode.NOT_READY, "Evaluation not ready")
        return job.evaluation

    async def artifact(self, artifact_id: str, frame: int | None) -> tuple[Path, str]:
        try:
            path = await self.artifacts.resolve(artifact_id, frame)
            media = await self.artifacts.media_type(artifact_id)
        except (FileNotFoundError, ValueError) as exc:
            raise not_found(ErrorCode.ARTIFACT_NOT_FOUND, "artifact") from exc
        return path, media


__all__ = ["JobService", "RequestError", "ServiceConfig"]
