"""SQLite job and artifact stores (ADR 0005)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kinesis.schemas import (
    ArtifactKind,
    DecisionChoice,
    HumanDecision,
    JobEvent,
    JobEventType,
    JobStatus,
    PlanMode,
    RepairJob,
    RepairType,
    SceneRef,
)
from kinesis.schemas.scope import AnimationSelection
from kinesis.storage.ids import new_id
from kinesis.storage.sqlite import Database, SqliteArtifactStore, SqliteJobStore
from kinesis.storage.store import ArtifactStore, IdempotencyConflict, InvalidTransition, JobStore

NOW = datetime(2026, 10, 7, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> SqliteJobStore:
    return SqliteJobStore(Database(tmp_path / "k.db"))


def make_job(selection: AnimationSelection, job_id: str = "job_000001") -> RepairJob:
    return RepairJob(
        job_id=job_id,
        idempotency_key="key-000001",
        repair_type=RepairType.FOOT_CONTACT,
        plan_mode=PlanMode.DETERMINISTIC,
        selection=selection,
        status=JobStatus.PENDING,
        created_at=NOW,
        updated_at=NOW,
    )


def event(job_id: str, kind: JobEventType = JobEventType.NODE_STARTED) -> JobEvent:
    return JobEvent(seq=1, job_id=job_id, ts=NOW, type=kind, node="extract_scope")


def test_ids_are_sortable_and_valid() -> None:
    a = new_id("job", now_ms=1)
    b = new_id("job", now_ms=2)
    assert a < b
    assert re.fullmatch(r"^[a-z0-9][a-z0-9_-]{5,63}$", a)
    with pytest.raises(ValueError, match="prefix"):
        new_id("Bad")


async def test_protocols_are_implemented(store: SqliteJobStore, tmp_path: Path) -> None:
    assert isinstance(store, JobStore)
    assert isinstance(SqliteArtifactStore(store.db, tmp_path / "jobs"), ArtifactStore)


async def test_scene_round_trip(store: SqliteJobStore) -> None:
    scene = SceneRef(
        scene_id="scn_000001",
        sha256="0" * 64,
        size_bytes=10,
        frame_start=1,
        frame_end=120,
        fps=24,
        blender_version="4.5.14",
        armatures=(),
    )
    await store.put_scene(scene)
    assert await store.get_scene("scn_000001") == scene
    assert await store.get_scene("scn_missing") is None


async def test_create_job_is_idempotent(
    store: SqliteJobStore, selection: AnimationSelection
) -> None:
    job = make_job(selection)
    created, fresh = await store.create_job(job, idempotency_key="key-000001", body_hash="h1")
    assert fresh
    again, fresh2 = await store.create_job(
        make_job(selection, "job_000002"), idempotency_key="key-000001", body_hash="h1"
    )
    assert not fresh2
    assert again.job_id == created.job_id
    with pytest.raises(IdempotencyConflict):
        await store.create_job(
            make_job(selection, "job_000003"), idempotency_key="key-000001", body_hash="h2"
        )
    assert await store.get_job("job_000002") is None


async def test_transitions_follow_the_state_machine(
    store: SqliteJobStore, selection: AnimationSelection
) -> None:
    await store.create_job(make_job(selection), idempotency_key="key-000001", body_hash="h")
    job = await store.transition("job_000001", JobStatus.RUNNING, message="started")
    assert job.status is JobStatus.RUNNING
    assert job.last_event_seq == 1
    with pytest.raises(InvalidTransition):
        await store.transition("job_000001", JobStatus.APPLYING)
    (ev,) = await store.list_events("job_000001", after_seq=0, limit=10)
    assert ev.type is JobEventType.STATUS_CHANGED
    assert ev.data == {"from": "PENDING", "to": "RUNNING"}


async def test_save_job_cannot_roll_back_status_or_seq(
    store: SqliteJobStore, selection: AnimationSelection
) -> None:
    stale = make_job(selection)
    await store.create_job(stale, idempotency_key="key-000001", body_hash="h")
    await store.transition("job_000001", JobStatus.RUNNING)
    await store.save_job(stale)  # still says PENDING, seq 0
    job = await store.get_job("job_000001")
    assert job is not None
    assert job.status is JobStatus.RUNNING
    assert job.last_event_seq == 1


async def test_events_are_sequenced_paginated_and_deduplicated(
    store: SqliteJobStore, selection: AnimationSelection
) -> None:
    await store.create_job(make_job(selection), idempotency_key="key-000001", body_hash="h")
    first = await store.append_event(event("job_000001"), dedupe_key="cb-1")
    dup = await store.append_event(event("job_000001"), dedupe_key="cb-1")
    assert dup.seq == first.seq == 1
    for _ in range(4):
        await store.append_event(event("job_000001"))
    page = await store.list_events("job_000001", after_seq=2, limit=2)
    assert [e.seq for e in page] == [3, 4]
    assert [e.seq for e in await store.list_events("job_000001", after_seq=4, limit=10)] == [5]


async def test_concurrent_appends_get_unique_seqs(
    store: SqliteJobStore, selection: AnimationSelection
) -> None:
    await store.create_job(make_job(selection), idempotency_key="key-000001", body_hash="h")
    events = await asyncio.gather(*(store.append_event(event("job_000001")) for _ in range(20)))
    assert sorted(e.seq for e in events) == list(range(1, 21))


async def test_decisions(store: SqliteJobStore) -> None:
    d = HumanDecision(
        job_id="job_000001", choice=DecisionChoice.B, model_recommended=None, decided_at=NOW
    )
    await store.record_decision(d)
    assert await store.list_decisions() == [d]


async def test_artifacts_resolve_by_id_and_frame(store: SqliteJobStore, tmp_path: Path) -> None:
    artifacts = SqliteArtifactStore(store.db, tmp_path / "jobs")
    job_dir = artifacts.job_dir("job_000001")
    (job_dir / "frames").mkdir(parents=True)
    for i in range(3):
        (job_dir / "frames" / f"crop_{i:04d}.jpg").write_bytes(b"jpg%d" % i)
    (job_dir / "m.json").write_text("{}", encoding="utf-8")
    seq = await artifacts.register(
        "job_000001",
        "frames",
        ArtifactKind.PREVIEW_FRAMES,
        "image/jpeg",
        frames=[f"frames/crop_{i:04d}.jpg" for i in range(3)],
        first_frame=35,
        frame_step=1,
    )
    assert (seq.frame_count, seq.first_frame, seq.uri) == (
        3,
        35,
        f"/v1/artifacts/{seq.artifact_id}",
    )
    assert (await artifacts.resolve(seq.artifact_id, frame=2)).read_bytes() == b"jpg2"
    with pytest.raises(FileNotFoundError):
        await artifacts.resolve(seq.artifact_id, frame=3)
    with pytest.raises(FileNotFoundError):
        await artifacts.resolve(seq.artifact_id)
    single = await artifacts.register(
        "job_000001", "m.json", ArtifactKind.METRICS_JSON, "application/json"
    )
    assert single.frame_count is None
    assert (await artifacts.resolve(single.artifact_id)).name == "m.json"
    assert await artifacts.media_type(single.artifact_id) == "application/json"
    with pytest.raises(FileNotFoundError):
        await artifacts.resolve("art_unknown01")


async def test_artifact_paths_cannot_escape(store: SqliteJobStore, tmp_path: Path) -> None:
    artifacts = SqliteArtifactStore(store.db, tmp_path / "jobs")
    with pytest.raises(ValueError, match="escapes"):
        await artifacts.register("job_000001", "../../etc/passwd", ArtifactKind.LOG, "text/plain")
    with pytest.raises(ValueError, match="invalid job id"):
        artifacts.job_dir("..")


async def test_sweep_removes_old_job_dirs(store: SqliteJobStore, tmp_path: Path) -> None:
    artifacts = SqliteArtifactStore(store.db, tmp_path / "jobs")
    artifacts.job_dir("job_000001").mkdir(parents=True)
    assert await artifacts.sweep(older_than_s=3600) == 0
    assert await artifacts.sweep(older_than_s=-1) == 1
    assert not artifacts.job_dir("job_000001").exists()
