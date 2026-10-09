"""Shared helpers for the benchmark tasks (``bench``, ``vlm-bakeoff``, ``demo``).

Everything here drives the real ``JobService`` with a real runner (host Blender or the worker
container). Nothing is simulated, so every number a benchmark prints traces to a real run.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kinesis.jobs.runner import JobRunner
from kinesis.jobs.service import JobService, ServiceConfig
from kinesis.providers.base import ModelProvider
from kinesis.schemas import CreateRepairJobRequest, DecisionChoice, DecisionRequest, JobStatus
from kinesis.schemas.job import JobEvent, RepairJob
from kinesis.storage.sqlite import Database, SqliteArtifactStore, SqliteJobStore

ROOT = Path(__file__).resolve().parent.parent


async def _chunks(path: Path, size: int = 1 << 20) -> AsyncIterator[bytes]:
    data = await asyncio.to_thread(path.read_bytes)
    for i in range(0, len(data), size):
        yield data[i : i + size]


def make_service(data_dir: Path, runner: JobRunner, provider: ModelProvider) -> JobService:
    db = Database(data_dir / "kinesis.db")
    return JobService(
        SqliteJobStore(db),
        SqliteArtifactStore(db, data_dir / "jobs"),
        runner,
        provider,
        ServiceConfig(data_dir=data_dir),
    )


@dataclass
class SceneRun:
    scene: str
    job: RepairJob
    job_dir: Path
    wall_s: float
    upload_s: float
    events: list[JobEvent] = field(default_factory=list)
    export_s: float | None = None

    def node_seconds(self) -> dict[str, float]:
        """Wall time per DAG node from its first NODE_STARTED to its last terminal event."""
        start: dict[str, float] = {}
        end: dict[str, float] = {}
        for e in self.events:
            if not e.node:
                continue
            t = e.ts.timestamp()
            if e.type.value == "NODE_STARTED":
                start.setdefault(e.node, t)
            elif e.type.value in ("NODE_SUCCEEDED", "NODE_FAILED", "NODE_SKIPPED"):
                end[e.node] = t
        return {n: round(end[n] - start[n], 3) for n in start if n in end}


async def run_scene(
    service: JobService,
    name: str,
    blend: Path,
    selection: dict[str, Any],
    *,
    decide: DecisionChoice | None = None,
    key: str | None = None,
) -> SceneRun:
    started = time.perf_counter()
    scene = await service.upload_scene(_chunks(blend))
    upload_s = time.perf_counter() - started
    body = CreateRepairJobRequest.model_validate(
        {"selection": {"scene_id": scene.scene_id, **selection}}
    )
    job, _ = await service.create_job(body, key or f"bench-{name}-{int(time.time() * 1000)}")
    await service.wait_idle()
    job = await service.get_job(job.job_id)
    wall_s = time.perf_counter() - started
    export_s = None
    if decide is not None and job.status is JobStatus.AWAITING_DECISION:
        t0 = time.perf_counter()
        await service.decide(job.job_id, DecisionRequest(choice=decide))
        await service.wait_idle()
        export_s = time.perf_counter() - t0
        job = await service.get_job(job.job_id)
    events = list(await service.store.list_events(job.job_id, after_seq=0, limit=10_000))
    return SceneRun(
        name, job, service.artifacts.job_dir(job.job_id), wall_s, upload_s, events, export_s
    )


def environment() -> dict[str, Any]:
    """Hardware and revision, recorded next to every benchmark number."""

    def git(*args: str) -> str:
        try:
            return subprocess.run(  # noqa: S603 - fixed argv
                ["git", *args],  # noqa: S607
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        except OSError:
            return ""

    gpu: str | None = None
    if shutil.which("nvidia-smi"):
        lines = (
            subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            .stdout.strip()
            .splitlines()
        )
        gpu = lines[0] if lines else None
    return {
        "commit": git("rev-parse", "--short", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "gpu": gpu,
        "python": platform.python_version(),
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")


__all__ = ["ROOT", "SceneRun", "environment", "make_service", "run_scene", "write_json"]
