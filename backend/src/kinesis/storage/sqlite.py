"""SQLite implementations of ``JobStore`` and ``ArtifactStore`` (ADR 0005).

- stdlib ``sqlite3`` in WAL mode, one connection guarded by a lock, called through
  ``asyncio.to_thread`` so the event loop never blocks on disk;
- every multi-statement change runs in one transaction;
- ``database is locked`` is retried 3 times with a short backoff, then surfaces as
  ``StorageError`` (FAILURE_MODES #17);
- aggregates are stored as Pydantic JSON. ``status`` and ``last_event_seq`` live in their own
  columns and always win over the JSON copy, so a stale ``save_job`` cannot roll them back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kinesis.errors import KinesisError
from kinesis.schemas import (
    JOB_TRANSITIONS,
    ArtifactKind,
    ArtifactReference,
    HumanDecision,
    JobEvent,
    JobEventType,
    JobStatus,
    RepairJob,
    SceneRef,
)
from kinesis.schemas.common import ErrorCode
from kinesis.storage.ids import new_id
from kinesis.storage.store import IdempotencyConflict, InvalidTransition

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
    scene_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY, status TEXT NOT NULL, last_seq INTEGER NOT NULL DEFAULT 0,
    data TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY, body_hash TEXT NOT NULL, job_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    job_id TEXT NOT NULL, seq INTEGER NOT NULL, dedupe_key TEXT, data TEXT NOT NULL,
    PRIMARY KEY (job_id, seq), UNIQUE (job_id, dedupe_key));
CREATE TABLE IF NOT EXISTS decisions (job_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, relpath TEXT NOT NULL,
    data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS artifacts_job ON artifacts(job_id);
"""


class StorageError(KinesisError):
    code = ErrorCode.STORAGE_ERROR


def utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    """One SQLite connection, serialized by a lock, used from worker threads."""

    def __init__(self, path: Path | str, *, busy_retries: int = 3) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None, timeout=1.0
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._busy_retries = busy_retries
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    def _tx[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        for attempt in range(self._busy_retries + 1):
            try:
                with self._lock:
                    self._conn.execute("BEGIN IMMEDIATE")
                    try:
                        out = fn(self._conn)
                    except BaseException:
                        self._conn.execute("ROLLBACK")
                        raise
                    self._conn.execute("COMMIT")
                    return out
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == self._busy_retries:
                    raise StorageError(f"sqlite: {exc}") from exc
                time.sleep(0.05 * (attempt + 1))
        raise AssertionError("unreachable")

    async def tx[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self._tx, fn)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------- job store


def _job_from_row(row: sqlite3.Row) -> RepairJob:
    job = RepairJob.model_validate_json(row["data"])
    return job.model_copy(
        update={"status": JobStatus(row["status"]), "last_event_seq": int(row["last_seq"])}
    )


def _insert_event(conn: sqlite3.Connection, event: JobEvent, dedupe_key: str | None) -> JobEvent:
    if dedupe_key is not None:
        row = conn.execute(
            "SELECT data FROM events WHERE job_id = ? AND dedupe_key = ?",
            (event.job_id, dedupe_key),
        ).fetchone()
        if row is not None:
            return JobEvent.model_validate_json(row["data"])
    job_row = conn.execute("SELECT last_seq FROM jobs WHERE job_id = ?", (event.job_id,)).fetchone()
    if job_row is None:
        raise KeyError(event.job_id)
    seq = int(job_row["last_seq"]) + 1
    stored = event.model_copy(update={"seq": seq})
    conn.execute(
        "INSERT INTO events (job_id, seq, dedupe_key, data) VALUES (?, ?, ?, ?)",
        (stored.job_id, seq, dedupe_key, stored.model_dump_json()),
    )
    conn.execute("UPDATE jobs SET last_seq = ? WHERE job_id = ?", (seq, stored.job_id))
    return stored


class SqliteJobStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def put_scene(self, scene: SceneRef) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT OR REPLACE INTO scenes (scene_id, data, created_at) VALUES (?, ?, ?)",
                (scene.scene_id, scene.model_dump_json(), utcnow().isoformat()),
            )

        await self.db.tx(op)

    async def get_scene(self, scene_id: str) -> SceneRef | None:
        def op(c: sqlite3.Connection) -> SceneRef | None:
            row = c.execute("SELECT data FROM scenes WHERE scene_id = ?", (scene_id,)).fetchone()
            return None if row is None else SceneRef.model_validate_json(row["data"])

        return await self.db.tx(op)

    async def create_job(
        self, job: RepairJob, *, idempotency_key: str, body_hash: str
    ) -> tuple[RepairJob, bool]:
        def op(c: sqlite3.Connection) -> tuple[RepairJob, bool]:
            row = c.execute(
                "SELECT body_hash, job_id FROM idempotency WHERE key = ?", (idempotency_key,)
            ).fetchone()
            if row is not None:
                if row["body_hash"] != body_hash:
                    raise IdempotencyConflict(idempotency_key)
                existing = c.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                return _job_from_row(existing), False
            now = job.created_at.isoformat()
            c.execute(
                "INSERT INTO jobs (job_id, status, last_seq, data, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, ?, ?)",
                (job.job_id, job.status.value, job.model_dump_json(), now, now),
            )
            c.execute(
                "INSERT INTO idempotency (key, body_hash, job_id) VALUES (?, ?, ?)",
                (idempotency_key, body_hash, job.job_id),
            )
            return job, True

        return await self.db.tx(op)

    async def get_job(self, job_id: str) -> RepairJob | None:
        def op(c: sqlite3.Connection) -> RepairJob | None:
            row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return None if row is None else _job_from_row(row)

        return await self.db.tx(op)

    async def save_job(self, job: RepairJob) -> None:
        """Persist the aggregate's fields. Status and seq are owned by ``transition`` and
        ``append_event``; the copies inside ``job`` are ignored."""

        def op(c: sqlite3.Connection) -> None:
            now = utcnow()
            cur = c.execute(
                "UPDATE jobs SET data = ?, updated_at = ? WHERE job_id = ?",
                (
                    job.model_copy(update={"updated_at": now}).model_dump_json(),
                    now.isoformat(),
                    job.job_id,
                ),
            )
            if cur.rowcount == 0:
                raise KeyError(job.job_id)

        await self.db.tx(op)

    async def transition(self, job_id: str, to: JobStatus, *, message: str = "") -> RepairJob:
        def op(c: sqlite3.Connection) -> RepairJob:
            row = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobStatus(row["status"])
            if to not in JOB_TRANSITIONS[current]:
                raise InvalidTransition(f"{current} -> {to}")
            now = utcnow()
            c.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
                (to.value, now.isoformat(), job_id),
            )
            _insert_event(
                c,
                JobEvent(
                    seq=1,
                    job_id=job_id,
                    ts=now,
                    type=JobEventType.STATUS_CHANGED,
                    message=message[:1000],
                    data={"from": current.value, "to": to.value},
                ),
                None,
            )
            refreshed = c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _job_from_row(refreshed)

        return await self.db.tx(op)

    async def append_event(self, event: JobEvent, *, dedupe_key: str | None = None) -> JobEvent:
        return await self.db.tx(lambda c: _insert_event(c, event, dedupe_key))

    async def list_events(self, job_id: str, *, after_seq: int, limit: int) -> Sequence[JobEvent]:
        def op(c: sqlite3.Connection) -> list[JobEvent]:
            rows = c.execute(
                "SELECT data FROM events WHERE job_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (job_id, after_seq, limit),
            ).fetchall()
            return [JobEvent.model_validate_json(r["data"]) for r in rows]

        return await self.db.tx(op)

    async def list_jobs_with_status(self, status: JobStatus) -> list[str]:
        def op(c: sqlite3.Connection) -> list[str]:
            rows = c.execute("SELECT job_id FROM jobs WHERE status = ?", (status.value,)).fetchall()
            return [r["job_id"] for r in rows]

        return await self.db.tx(op)

    async def record_decision(self, decision: HumanDecision) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO decisions (job_id, data) VALUES (?, ?)",
                (decision.job_id, decision.model_dump_json()),
            )

        await self.db.tx(op)

    async def list_decisions(self) -> Sequence[HumanDecision]:
        def op(c: sqlite3.Connection) -> list[HumanDecision]:
            rows = c.execute("SELECT data FROM decisions ORDER BY rowid").fetchall()
            return [HumanDecision.model_validate_json(r["data"]) for r in rows]

        return await self.db.tx(op)


# ---------------------------------------------------------------- artifact store


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class SqliteArtifactStore:
    """Files under ``<jobs_root>/<job_id>/``, addressed only by ``artifact_id``.

    A frame sequence is registered as a list of files; ``resolve(id, frame=i)`` returns the
    i-th. Every resolved path is checked to stay inside ``jobs_root`` (FAILURE_MODES #23).
    """

    def __init__(self, db: Database, jobs_root: Path) -> None:
        self.db = db
        self.jobs_root = jobs_root.resolve()

    def job_dir(self, job_id: str) -> Path:
        path = (self.jobs_root / job_id).resolve()
        if not path.is_relative_to(self.jobs_root) or path == self.jobs_root:
            raise ValueError(f"invalid job id {job_id!r}")
        return path

    def _inside(self, job_id: str, relpath: str) -> Path:
        path = (self.job_dir(job_id) / relpath).resolve()
        if not path.is_relative_to(self.job_dir(job_id)):
            raise ValueError(f"path escapes job directory: {relpath!r}")
        return path

    async def register(
        self,
        job_id: str,
        relpath: str,
        kind: ArtifactKind,
        media_type: str,
        *,
        frames: Sequence[str] = (),
        first_frame: int | None = None,
        frame_step: int | None = None,
    ) -> ArtifactReference:
        """Register one file, or with ``frames`` a sequence of files (``relpath`` names the
        sequence's directory)."""

        def build() -> tuple[ArtifactReference, dict[str, Any]]:
            artifact_id = new_id("art")
            files = [self._inside(job_id, f) for f in frames] or [self._inside(job_id, relpath)]
            digest = hashlib.sha256()
            size = 0
            for f in files:
                digest.update(_sha256_file(f).encode())
                size += f.stat().st_size
            ref = ArtifactReference(
                artifact_id=artifact_id,
                kind=kind,
                media_type=media_type,
                sha256=digest.hexdigest() if frames else _sha256_file(files[0]),
                size_bytes=size,
                frame_count=len(frames) if frames else None,
                first_frame=first_frame if frames else None,
                frame_step=frame_step if frames else None,
                uri=f"/v1/artifacts/{artifact_id}",
            )
            return ref, {"ref": ref.model_dump(mode="json"), "files": list(frames) or [relpath]}

        ref, record = await asyncio.to_thread(build)

        def op(c: sqlite3.Connection) -> None:

            c.execute(
                "INSERT INTO artifacts (artifact_id, job_id, relpath, data, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (ref.artifact_id, job_id, relpath, json.dumps(record), utcnow().isoformat()),
            )

        await self.db.tx(op)
        return ref

    async def resolve(self, artifact_id: str, frame: int | None = None) -> Path:

        def op(c: sqlite3.Connection) -> tuple[str, dict[str, Any]] | None:
            row = c.execute(
                "SELECT job_id, data FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return None if row is None else (row["job_id"], json.loads(row["data"]))

        found = await self.db.tx(op)
        if found is None:
            raise FileNotFoundError(artifact_id)
        job_id, record = found
        files: list[str] = record["files"]
        is_sequence = record["ref"].get("frame_count") is not None
        if is_sequence:
            if frame is None or not 0 <= frame < len(files):
                raise FileNotFoundError(f"{artifact_id} frame {frame}")
            rel = files[frame]
        else:
            if frame is not None:
                raise FileNotFoundError(f"{artifact_id} is not a frame sequence")
            rel = files[0]
        path = self._inside(job_id, rel)
        if not path.is_file():
            raise FileNotFoundError(artifact_id)
        return path

    async def media_type(self, artifact_id: str) -> str:

        def op(c: sqlite3.Connection) -> str | None:
            row = c.execute(
                "SELECT data FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
            return None if row is None else str(json.loads(row["data"])["ref"]["media_type"])

        value = await self.db.tx(op)
        if value is None:
            raise FileNotFoundError(artifact_id)
        return value

    async def sweep(self, older_than_s: float) -> int:
        """Delete job directories (and their artifact rows) not modified for ``older_than_s``."""
        cutoff = time.time() - older_than_s
        removed = 0
        if not self.jobs_root.exists():
            return 0
        for job_dir in self.jobs_root.iterdir():
            if job_dir.is_dir() and job_dir.stat().st_mtime < cutoff:
                await asyncio.to_thread(shutil.rmtree, job_dir, True)

                def forget(c: sqlite3.Connection, job_id: str = job_dir.name) -> None:
                    c.execute("DELETE FROM artifacts WHERE job_id = ?", (job_id,))

                await self.db.tx(forget)
                removed += 1
        return removed


__all__ = ["Database", "SqliteArtifactStore", "SqliteJobStore", "StorageError", "utcnow"]
