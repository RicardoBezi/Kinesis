"""A fake Nebius AI Jobs API + token exchange, for contract tests without the cloud.

It mirrors the documented REST contract (docs.nebius.com, verified 2026-10-09):
- ``POST https://auth.eu.nebius.com/oauth2/token/exchange`` (RFC 8693 form fields) -> token;
- ``POST /ai/v1/jobs`` -> an Operation whose ``resourceId`` is the job id;
- ``GET /ai/v1/jobs/{id}`` -> ``status.state`` advancing one step per poll:
  PROVISIONING -> STARTING -> IMAGE_PULLING -> RUNNING -> COMPLETED | FAILED | ERROR;
- ``POST /ai/v1/jobs/{id}:cancel`` -> CANCELLED.

"Running the container" means syncing the job's bucket prefix (the ``/work`` volume) into a
temporary directory, running ``FakeJobRunner`` there, and syncing the results back, so the
runner under test sees exactly what a real job would leave in the bucket. Session jobs
(design B) process ``queue/*.request.json`` on every poll while RUNNING and complete on
``queue/STOP``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import shlex
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from kinesis.errors import WorkerCrashed, WorkerTimeout
from kinesis.jobs.nebius.store import MemoryObjectStore, download_prefix
from kinesis.jobs.runner import WorkerInvocation
from kinesis.schemas.worker import WorkerCommand
from kinesis.testing.fake_runner import FakeJobRunner

PROGRESSION = ("PROVISIONING", "STARTING", "IMAGE_PULLING", "RUNNING")


class Outcome(StrEnum):
    NORMAL = "NORMAL"  # the container decides: COMPLETED, or FAILED on a non-zero exit
    ERROR = "ERROR"  # the platform fails the job (e.g. quota) before the container runs
    HANG = "HANG"  # stays RUNNING until cancelled


@dataclass
class FakeJob:
    job_id: str
    body: dict[str, Any]
    outcome: Outcome
    state: str = "PROVISIONING"
    polls: int = 0
    message: str = ""
    started: bool = False
    finished: bool = False

    @property
    def spec(self) -> dict[str, Any]:
        spec: dict[str, Any] = self.body["spec"]
        return spec

    @property
    def prefix(self) -> str:
        return str(self.spec["volumes"][0]["sourcePath"])

    @property
    def is_session(self) -> bool:
        return "containerCommand" in self.spec


@dataclass
class FakeNebius:
    store: MemoryObjectStore = field(default_factory=MemoryObjectStore)
    worker: FakeJobRunner = field(default_factory=FakeJobRunner)
    outcomes: list[Outcome] = field(default_factory=list)  # consumed per created job
    create_failures: list[int] = field(default_factory=list)  # HTTP statuses before success
    expire_token_once: bool = False
    jobs: dict[str, FakeJob] = field(default_factory=dict)
    exchanges: int = 0
    cancelled: list[str] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _valid_token: str = ""

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------ HTTP

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if request.url.path.endswith("/oauth2/token/exchange"):
            return self._exchange(request)
        if (
            request.headers.get("authorization") != f"Bearer {self._valid_token}"
            or not self._valid_token
        ):
            return httpx.Response(401, json={"message": "unauthenticated"})
        if self.expire_token_once:
            self.expire_token_once = False
            self._valid_token = "revoked"  # noqa: S105 - a fake token
            return httpx.Response(401, json={"message": "token expired"})
        path = request.url.path
        if request.method == "POST" and path == "/ai/v1/jobs":
            return self._create(json.loads(request.content))
        if request.method == "POST" and path.endswith(":cancel"):
            job = self.jobs.get(path.split("/")[-1].removesuffix(":cancel"))
            if job is None:
                return httpx.Response(404, json={"message": "not found"})
            if not job.finished:
                job.state, job.finished = "CANCELLED", True
            self.cancelled.append(job.job_id)
            return httpx.Response(200, json={"id": "op-cancel", "resourceId": job.job_id})
        if request.method == "GET" and path.startswith("/ai/v1/jobs/"):
            job = self.jobs.get(path.split("/")[-1])
            if job is None:
                return httpx.Response(404, json={"message": "not found"})
            # Report the current state, then move on: every state is observable once.
            body = self._job_json(job)
            await self._advance(job)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"message": f"no route {path}"})

    def _exchange(self, request: httpx.Request) -> httpx.Response:
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        expected = {
            "grant_type": "urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Atoken-exchange",
            "subject_token_type": "urn%3Aietf%3Aparams%3Aoauth%3Atoken-type%3Ajwt",
        }
        if (
            any(form.get(k) != v for k, v in expected.items())
            or form.get("subject_token", "").count(".") != 2
        ):
            return httpx.Response(400, json={"error": "invalid_request"})
        self.exchanges += 1
        self._valid_token = f"tok-{self.exchanges}"
        return httpx.Response(
            200,
            json={
                "access_token": self._valid_token,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "token_type": "Bearer",
                "expires_in": 43200,
            },
        )

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        if self.create_failures:
            return httpx.Response(self.create_failures.pop(0), json={"message": "try again"})
        spec = body.get("spec") or {}
        volumes = spec.get("volumes") or []
        problems = [
            "metadata.parentId" if not (body.get("metadata") or {}).get("parentId") else "",
            "spec.image" if not spec.get("image") else "",
            "spec.args must be a string" if not isinstance(spec.get("args"), str) else "",
            "volume at /work" if not volumes or volumes[0].get("containerPath") != "/work" else "",
            "timeout like '3600s'" if not str(spec.get("timeout", "")).endswith("s") else "",
        ]
        problems = [p for p in problems if p]
        if problems:
            return httpx.Response(400, json={"message": f"invalid job spec: {problems}"})
        job_id = f"aijob-{next(self._ids):04d}"
        outcome = self.outcomes.pop(0) if self.outcomes else Outcome.NORMAL
        self.jobs[job_id] = FakeJob(job_id, body, outcome)
        return httpx.Response(
            200, json={"id": f"op-{job_id}", "resourceId": job_id, "status": {"code": 0}}
        )

    def _job_json(self, job: FakeJob) -> dict[str, Any]:
        status: dict[str, Any] = {"state": job.state, "stateDetails": {"message": job.message}}
        if job.started:
            status["startedAt"] = "2026-10-09T00:00:00Z"
        if job.finished and job.started:
            status["finishedAt"] = f"2026-10-09T00:00:{min(59, 10 + job.polls):02d}Z"
        return {"metadata": {"id": job.job_id}, "spec": job.spec, "status": status}

    # ------------------------------------------------------------ "the container"

    async def _advance(self, job: FakeJob) -> None:
        if job.finished:
            return
        job.polls += 1
        if job.state != "RUNNING":
            index = PROGRESSION.index(job.state) if job.state in PROGRESSION else 0
            if job.outcome is Outcome.ERROR and job.state == "PROVISIONING":
                job.state, job.message, job.finished = "ERROR", "quota exceeded for platform", True
                return
            job.state = PROGRESSION[min(index + 1, len(PROGRESSION) - 1)]
            job.started = job.state == "RUNNING"
            return
        if job.outcome is Outcome.HANG:
            return
        if job.is_session:
            await self._serve_session(job)
            return
        args = shlex.split(job.spec["args"])
        code = await self._execute(
            job.prefix, args[args.index("--command") + 1], args[args.index("--spec") + 1]
        )
        job.finished = True
        job.state = "COMPLETED" if code == 0 else "FAILED"
        job.message = "" if code == 0 else f"container exited with code {code}"

    async def _serve_session(self, job: FakeJob) -> None:
        queue = f"{job.prefix}queue/"
        keys = await self.store.list_keys(queue)
        for key in sorted(k for k in keys if k.endswith(".request.json")):
            done = key.replace(".request.json", ".done.json")
            if done in keys:
                continue
            request = json.loads(self.store.objects[key])
            code = await self._execute(job.prefix, request["command"], request["spec"])
            await self.store.put_bytes(done, json.dumps({"exit_code": code}).encode())
        if f"{queue}STOP" in keys:
            job.state, job.finished = "COMPLETED", True

    async def _execute(self, prefix: str, command: str, spec: str) -> int:
        """Run one worker step against the bucket prefix; returns the container exit code."""
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            await download_prefix(self.store, prefix, work)
            spec_rel = spec.removeprefix("/work/")
            spec_data = json.loads((work / spec_rel).read_text(encoding="utf-8"))
            invocation = WorkerInvocation(
                job_dir=work,
                command=WorkerCommand(command),
                spec_relpath=spec_rel,
                result_relpath=spec_data["result_path"],
                timeout_s=300,
            )
            try:
                outcome = await self.worker.run(invocation)
                code = outcome.exit_code
            except (WorkerCrashed, WorkerTimeout):
                code = 3
            files = await asyncio.to_thread(
                lambda: {
                    p.relative_to(work).as_posix(): p.read_bytes()
                    for p in work.rglob("*")
                    if p.is_file()
                }
            )
            for rel, data in files.items():
                await self.store.put_bytes(f"{prefix}{rel}", data)
            return code


__all__ = ["FakeJob", "FakeNebius", "Outcome"]
