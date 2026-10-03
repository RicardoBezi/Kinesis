"""Persistence interfaces (ADR 0005). SQLite implements JobStore in Phase 3."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from kinesis.schemas import (
    ArtifactKind,
    ArtifactReference,
    HumanDecision,
    JobEvent,
    JobStatus,
    RepairJob,
    SceneRef,
)


class IdempotencyConflict(Exception):
    """Same Idempotency-Key, different request body."""


class InvalidTransition(Exception):
    """Status change not allowed by JOB_TRANSITIONS."""


@runtime_checkable
class JobStore(Protocol):
    """Source of truth for scenes, jobs, events and decisions.

    Contract:
    - ``create_job`` is atomic with the idempotency key: returns ``(job, created)``; replaying
      the same key + body hash returns the existing job with ``created=False``; same key with
      a different body hash raises ``IdempotencyConflict``;
    - ``transition`` enforces ``JOB_TRANSITIONS`` and appends a STATUS_CHANGED event in the
      same transaction;
    - ``append_event`` assigns ``seq`` (monotonic per job) and is idempotent on
      ``(job_id, dedupe_key)`` so duplicate status callbacks are dropped.
    """

    async def put_scene(self, scene: SceneRef) -> None: ...

    async def get_scene(self, scene_id: str) -> SceneRef | None: ...

    async def create_job(
        self, job: RepairJob, *, idempotency_key: str, body_hash: str
    ) -> tuple[RepairJob, bool]: ...

    async def get_job(self, job_id: str) -> RepairJob | None: ...

    async def save_job(self, job: RepairJob) -> None: ...

    async def transition(self, job_id: str, to: JobStatus, *, message: str = "") -> RepairJob: ...

    async def append_event(self, event: JobEvent, *, dedupe_key: str | None = None) -> JobEvent: ...

    async def list_events(
        self, job_id: str, *, after_seq: int, limit: int
    ) -> Sequence[JobEvent]: ...

    async def record_decision(self, decision: HumanDecision) -> None: ...

    async def list_decisions(self) -> Sequence[HumanDecision]: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Files under ``<data_dir>/jobs/<job_id>/``, addressed by artifact id."""

    def job_dir(self, job_id: str) -> Path: ...

    async def register(
        self, job_id: str, relpath: str, kind: ArtifactKind, media_type: str
    ) -> ArtifactReference: ...

    async def resolve(self, artifact_id: str, frame: int | None = None) -> Path: ...

    async def sweep(self, older_than_s: float) -> int: ...
