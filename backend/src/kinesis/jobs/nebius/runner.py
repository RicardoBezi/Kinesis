"""Nebius Serverless Jobs runners (Phase 6). Both implement ``JobRunner``.

**Design A, ``NebiusJobRunner``: one Nebius job per worker step.** Each ``run()`` uploads
the step's inputs under the job's bucket prefix, submits a job that runs the worker image with
the usual ``--command <enum> --spec /work/...`` (the prefix is mounted at ``/work``), polls
until a terminal state, then downloads the outputs. Simple and stateless, but every step pays
VM provisioning and a 2 GB image pull (EXPECTED to be minutes; not measured: live runs are
blocked).

**Design B, ``NebiusSessionRunner``: one Nebius job per Kinesis job.** The first step starts a
long-lived "session" job running ``blender/worker/session.py``, which watches
``/work/queue/`` for request files and runs each step with the same fixed Blender argv. Each
``run()`` writes a request object and polls for its ``.done.json``. Start-up is paid once per
Kinesis job; the cost is an idle VM between steps and reliance on the bucket mount seeing
new objects promptly (EXPECTED; to be verified live).

Both: a client-side watchdog cancels jobs that run longer than ``watchdog_s`` (Nebius' minimum
timeout is 1 h), a ``SpendGuard`` enforces the budget, cancelling the Kinesis job cancels the
Nebius job, and per-state timestamps plus a cost estimate are written to
``work/<node>.nebius.json`` and logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kinesis.errors import WorkerCrashed, WorkerTimeout
from kinesis.jobs.local import CRASH_EXIT, HANDLED_FAILURE_EXIT, SCENE_RELPATH
from kinesis.jobs.nebius.api import JobState, JobStatus, NebiusApiError, NebiusJobsClient
from kinesis.jobs.nebius.config import GIB, NebiusJobConfig, SpendGuard
from kinesis.jobs.nebius.store import ObjectStore, download_prefix, upload_files
from kinesis.jobs.runner import WorkerInvocation, WorkerOutcome, assert_inside
from kinesis.orchestration.dag import RetryPolicy
from kinesis.orchestration.retry import call_with_retry

log = logging.getLogger("kinesis.nebius")

SESSION_SCRIPT = "/opt/kinesis/worker/session.py"
SESSION_PYTHON = "/opt/blender/4.5/python/bin/python3.11"
SUBMIT_RETRY = RetryPolicy(max_attempts=3, base_s=1.0, cap_s=8.0)  # FAILURE_MODES #19


@dataclass
class RunTrace:
    """Per-state first-seen times and the cost estimate for one Nebius job."""

    job_id: str
    submitted_at: float
    states: dict[str, float] = field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    estimated_cost_usd: float | None = None

    def runtime_s(self) -> float | None:
        if self.started_at and self.finished_at:
            return max(0.0, (self.finished_at - self.started_at).total_seconds())
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "nebius_job_id": self.job_id,
            "states_s_after_submit": {
                k: round(v - self.submitted_at, 3) for k, v in self.states.items()
            },
            "runtime_s": self.runtime_s(),
            "estimated_cost_usd": self.estimated_cost_usd,
        }


class _NebiusBase:
    name = "nebius"

    def __init__(
        self,
        client: NebiusJobsClient,
        store: ObjectStore,
        config: NebiusJobConfig,
        guard: SpendGuard,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_min_s: float = 2.0,
        poll_max_s: float = 15.0,
    ) -> None:
        self.client = client
        self.store = store
        self.config = config
        self.guard = guard
        self.sleep = sleep
        self.clock = clock
        self.poll_min_s = poll_min_s
        self.poll_max_s = poll_max_s
        self._uploaded: dict[str, tuple[int, int]] = {}  # key -> (size, mtime_ns)

    async def upload_inputs(self, job_dir: Path, relpaths: list[str], prefix: str) -> None:
        """Upload inputs, skipping files already uploaded unchanged (the scene, mostly)."""
        pending = []
        for rel in relpaths:
            stat = (job_dir / rel).stat()
            if self._uploaded.get(prefix + rel) != (stat.st_size, stat.st_mtime_ns):
                pending.append((rel, (stat.st_size, stat.st_mtime_ns)))
        await upload_files(self.store, job_dir, [rel for rel, _ in pending], prefix)
        for rel, sig in pending:
            self._uploaded[prefix + rel] = sig

    # ------------------------------------------------------------ job spec

    def job_body(self, name: str, prefix: str, *, command: str | None, args: str) -> dict[str, Any]:
        c = self.config
        spec: dict[str, Any] = {
            "image": c.image,
            "platform": c.platform,
            "preset": c.preset,
            "args": args,
            "timeout": f"{c.job_timeout_s}s",
            "restartAttempts": "0",
            "disk": {"type": "NETWORK_SSD", "sizeBytes": str(c.disk_gib * GIB)},
            "volumes": [
                {
                    "source": c.bucket,
                    "sourcePath": prefix,
                    "containerPath": "/work",
                    "mode": "READ_WRITE",
                    "s3Config": {
                        "endpoint": c.s3_endpoint,
                        "region": c.region,
                        "credentials": {
                            "accessKeyId": c.s3_access_key_id,
                            "secretAccessKey": c.s3_secret_access_key,
                        },
                    },
                }
            ],
            "pricingModel": {"followsSpotPrice": {}} if c.preemptible else {"onDemand": {}},
            "preemptible": c.preemptible,
        }
        if command is not None:
            spec["containerCommand"] = command
        if c.registry_username and c.registry_password:
            spec["registryCredentials"] = {
                "username": c.registry_username,
                "password": c.registry_password,
            }
        return {"metadata": {"parentId": c.project_id, "name": name[:63]}, "spec": spec}

    async def submit(self, body: dict[str, Any]) -> str:
        return await call_with_retry(
            lambda: self.client.create_job(body), SUBMIT_RETRY, sleep=self.sleep
        )

    async def cancel_quietly(self, job_id: str) -> None:
        with contextlib.suppress(NebiusApiError):
            await self.client.cancel_job(job_id)

    async def poll_once(self, trace: RunTrace) -> JobStatus:
        status = await call_with_retry(
            lambda: self.client.get_job(trace.job_id), SUBMIT_RETRY, sleep=self.sleep
        )
        trace.states.setdefault(status.state.value, self.clock())
        trace.started_at = status.started_at or trace.started_at
        trace.finished_at = status.finished_at or trace.finished_at
        return status

    def write_trace(self, job_dir: Path, spec_relpath: str, trace: RunTrace) -> None:
        rel = spec_relpath.replace(".spec.json", ".nebius.json")
        with contextlib.suppress(OSError, ValueError):
            path = assert_inside(job_dir, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(trace.to_json(), indent=1), encoding="utf-8")
        log.info("nebius.run", extra=trace.to_json())

    async def health_check(self) -> tuple[bool, str]:
        return (
            True,
            f"nebius runner ({self.config.platform}/{self.config.preset}, "
            f"image {self.config.image})",
        )


class NebiusJobRunner(_NebiusBase):
    """Design A: one Nebius job per worker step."""

    async def run(self, invocation: WorkerInvocation) -> WorkerOutcome:
        job_dir = invocation.job_dir
        prefix = self.config.prefix_for(job_dir)
        result_path = assert_inside(job_dir, invocation.result_relpath)
        result_path.unlink(missing_ok=True)
        await self.store.delete_prefix(f"{prefix}{invocation.result_relpath}")
        await self.upload_inputs(job_dir, _inputs(job_dir, invocation), prefix)

        reserved = self.guard.reserve(self.config.watchdog_s)
        started = self.clock()
        args = shlex.join(
            ["--command", invocation.command.value, "--spec", f"/work/{invocation.spec_relpath}"]
        )
        node = Path(invocation.spec_relpath).name.removesuffix(".spec.json")
        name = f"kinesis-{job_dir.name}-{node}"
        try:
            job_id = await self.submit(self.job_body(name, prefix, command=None, args=args))
        except BaseException:
            self.guard.settle(reserved, 0.0)
            raise
        trace = RunTrace(job_id, started)
        deadline = started + min(invocation.timeout_s, self.config.watchdog_s)
        try:
            status = await self._wait(trace, deadline, invocation)
        except BaseException:
            trace.estimated_cost_usd = self.guard.settle(
                reserved, trace.runtime_s() or (self.clock() - started)
            )
            self.write_trace(job_dir, invocation.spec_relpath, trace)
            raise
        trace.estimated_cost_usd = self.guard.settle(reserved, trace.runtime_s())
        self.write_trace(job_dir, invocation.spec_relpath, trace)

        await download_prefix(self.store, prefix, job_dir, skip={SCENE_RELPATH})
        if status.state is not JobState.COMPLETED:
            if status.state is JobState.FAILED and result_path.exists():
                # The worker caught its own error and wrote a WorkerFailure (exit 2): the
                # caller parses it like a local handled failure.
                return WorkerOutcome(
                    result_path, int((self.clock() - started) * 1000), HANDLED_FAILURE_EXIT
                )
            raise WorkerCrashed(
                f"{invocation.command.value}: Nebius job {job_id} ended {status.state.value}"
                + (f" ({status.message})" if status.message else "")
            )
        return WorkerOutcome(result_path, int((self.clock() - started) * 1000), 0)

    async def _wait(self, trace: RunTrace, deadline: float, inv: WorkerInvocation) -> JobStatus:
        delay = self.poll_min_s
        try:
            while True:
                status = await self.poll_once(trace)
                if status.state.is_terminal:
                    return status
                if self.clock() >= deadline:
                    await self.cancel_quietly(trace.job_id)
                    raise WorkerTimeout(
                        f"{inv.command.value}: Nebius job {trace.job_id} still "
                        f"{status.state.value} "
                        f"after {deadline - trace.submitted_at:.0f} s; cancelled by the watchdog"
                    )
                await self.sleep(delay)
                delay = min(self.poll_max_s, delay * 1.5)
        except asyncio.CancelledError:
            await asyncio.shield(self.cancel_quietly(trace.job_id))  # never leave a VM running
            raise


@dataclass
class _Session:
    job_id: str
    trace: RunTrace
    reserved: float
    counter: int = 0


class NebiusSessionRunner(_NebiusBase):
    """Design B: one long-lived Nebius job per Kinesis job, fed through ``/work/queue``."""

    def __init__(self, *args: Any, idle_timeout_s: float = 600.0, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.idle_timeout_s = idle_timeout_s
        self._sessions: dict[str, _Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _session(self, job_dir: Path) -> _Session:
        lock = self._locks.setdefault(job_dir.name, asyncio.Lock())
        async with lock:
            existing = self._sessions.get(job_dir.name)
            if existing is not None:
                status = await self.poll_once(existing.trace)
                if not status.state.is_terminal:
                    return existing
                self._sessions.pop(job_dir.name, None)  # it idled out: start a new one
            prefix = self.config.prefix_for(job_dir)
            await self.upload_inputs(job_dir, [SCENE_RELPATH], prefix)
            reserved = self.guard.reserve(self.config.watchdog_s)
            args = shlex.join([SESSION_SCRIPT, "--idle-timeout", f"{self.idle_timeout_s:g}"])
            try:
                job_id = await self.submit(
                    self.job_body(
                        f"kinesis-session-{job_dir.name}", prefix, command=SESSION_PYTHON, args=args
                    )
                )
            except BaseException:
                self.guard.settle(reserved, 0.0)
                raise
            session = _Session(job_id, RunTrace(job_id, self.clock()), reserved)
            self._sessions[job_dir.name] = session
            return session

    async def run(self, invocation: WorkerInvocation) -> WorkerOutcome:
        job_dir = invocation.job_dir
        prefix = self.config.prefix_for(job_dir)
        session = await self._session(job_dir)
        session.counter += 1
        request_id = (
            f"{Path(invocation.spec_relpath).name.removesuffix('.spec.json')}-{session.counter}"
        )
        result_path = assert_inside(job_dir, invocation.result_relpath)
        result_path.unlink(missing_ok=True)
        await self.store.delete_prefix(f"{prefix}{invocation.result_relpath}")
        await self.upload_inputs(job_dir, [invocation.spec_relpath], prefix)
        request = {"command": invocation.command.value, "spec": f"/work/{invocation.spec_relpath}"}
        await self.store.put_bytes(
            f"{prefix}queue/{request_id}.request.json", json.dumps(request).encode()
        )

        started = self.clock()
        deadline = started + min(invocation.timeout_s, self.config.watchdog_s)
        done_key = f"{prefix}queue/{request_id}.done.json"
        delay = self.poll_min_s
        try:
            while True:
                done = await self.store.get_bytes(done_key)
                if done is not None:
                    break
                status = await self.poll_once(session.trace)
                if status.state.is_terminal:
                    self._sessions.pop(job_dir.name, None)
                    raise WorkerCrashed(
                        f"{invocation.command.value}: session job {session.job_id} ended "
                        f"{status.state.value} before answering"
                        + (f" ({status.message})" if status.message else "")
                    )
                if self.clock() >= deadline:
                    raise WorkerTimeout(
                        f"{invocation.command.value}: no answer from session {session.job_id} "
                        f"after {deadline - started:.0f} s"
                    )
                await self.sleep(delay)
                delay = min(self.poll_max_s, delay * 1.5)
        except (asyncio.CancelledError, WorkerTimeout):
            await asyncio.shield(self.close(job_dir, cancel=True))
            raise

        exit_code = int(json.loads(done).get("exit_code", CRASH_EXIT))
        await download_prefix(self.store, prefix, job_dir, skip={SCENE_RELPATH})
        elapsed = int((self.clock() - started) * 1000)
        if exit_code != 0 and not (exit_code == HANDLED_FAILURE_EXIT and result_path.exists()):
            raise WorkerCrashed(
                f"{invocation.command.value} exited with {exit_code} in session {session.job_id}"
            )
        return WorkerOutcome(result_path, elapsed, exit_code)

    async def close(self, job_dir: Path, *, cancel: bool = False) -> None:
        """End the session: ask the worker loop to stop (or cancel it) and settle the cost."""
        session = self._sessions.pop(job_dir.name, None)
        if session is None:
            return
        prefix = self.config.prefix_for(job_dir)
        if cancel:
            await self.cancel_quietly(session.job_id)
        else:
            await self.store.put_bytes(f"{prefix}queue/STOP", b"")
        with contextlib.suppress(Exception):
            await self.poll_once(session.trace)
        runtime = session.trace.runtime_s() or (self.clock() - session.trace.submitted_at)
        session.trace.estimated_cost_usd = self.guard.settle(session.reserved, runtime)
        self.write_trace(job_dir, "work/session.spec.json", session.trace)


def _inputs(job_dir: Path, inv: WorkerInvocation) -> list[str]:
    """The scene, the spec, and (for export) the candidate .blend the spec points at."""
    files = [SCENE_RELPATH, inv.spec_relpath]
    try:
        spec = json.loads(assert_inside(job_dir, inv.spec_relpath).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return files
    extra = spec.get("candidate_blend")
    if isinstance(extra, str) and (job_dir / extra).exists():
        files.append(extra)
    return files


__all__ = ["NebiusJobRunner", "NebiusSessionRunner", "RunTrace"]
