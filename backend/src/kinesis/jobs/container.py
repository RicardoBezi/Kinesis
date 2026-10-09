"""ContainerJobRunner: the worker in its container image, one `docker run` per command.

This is the unit Nebius Serverless Jobs will execute (Phase 6): same image, same fixed argv,
same job-directory contract. Running it locally proves the containerized worker produces the
same results as the host Blender before any cloud code depends on it.

Isolation per run: no network, read-only root filesystem, a tmpfs for Blender's config, and
the job directory as the only writable mount. Cancelling or timing out force-removes the
container, because killing the ``docker`` CLI alone would leave Blender running.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from pathlib import Path

from kinesis.errors import WorkerCrashed
from kinesis.jobs.local import AbortHook, LocalJobRunner
from kinesis.jobs.runner import WorkerInvocation, WorkerOutcome, assert_inside

DEFAULT_IMAGE = "kinesis-worker:4.5.14"
DOCKER_RUN_ERRORS = (125, 126, 127)


class ContainerJobRunner(LocalJobRunner):
    name = "container"

    def __init__(self, image: str = DEFAULT_IMAGE, docker: str = "docker") -> None:
        super().__init__(Path(docker))
        self.image = image
        self.docker = docker

    def container_argv(self, invocation: WorkerInvocation, container: str) -> list[str]:
        job_dir = invocation.job_dir.resolve()
        spec = assert_inside(job_dir, invocation.spec_relpath).relative_to(job_dir).as_posix()
        # On Linux, run as the caller so files written into the bind mount stay theirs.
        # (Docker Desktop on Windows/macOS maps ownership itself.)
        if sys.platform == "win32":
            user: list[str] = []
        else:
            user = ["--user", f"{os.getuid()}:{os.getgid()}"]
        return [
            self.docker, "run", "--rm", "--name", container,
            "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,size=512m",  # noqa: S108 - a tmpfs inside the container
            "--security-opt", "no-new-privileges", "--cpus", "4", "--memory", "4g",
            *user,
            "-v", f"{job_dir}:/work",
            self.image,
            "--command", invocation.command.value, "--spec", f"/work/{spec}",
        ]  # fmt: skip

    def launch(self, invocation: WorkerInvocation) -> tuple[list[str], AbortHook | None]:
        container = f"kinesis-{secrets.token_hex(6)}"

        async def remove() -> None:
            proc = await asyncio.create_subprocess_exec(
                self.docker, "rm", "-f", container,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )  # fmt: skip
            await proc.wait()

        return self.container_argv(invocation, container), remove

    async def run(self, invocation: WorkerInvocation) -> WorkerOutcome:
        try:
            return await super().run(invocation)
        except WorkerCrashed as exc:
            # 125-127 mean `docker run` itself failed (daemon, image, mount or flag error), so
            # Blender never started. Docker's one-line reason is safe and essential to surface.
            if not any(f"exited with {code};" in exc.message for code in DOCKER_RUN_ERRORS):
                raise
            log = invocation.job_dir / Path(invocation.spec_relpath).with_suffix("").with_suffix(
                ".log"
            )
            reason = log.read_text(errors="replace").strip()[-400:] if log.exists() else ""
            raise WorkerCrashed(f"{exc.message}: docker: {reason}", node=exc.node) from exc

    async def health_check(self) -> tuple[bool, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, "image", "inspect", "--format", "{{.Id}}", self.image,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            )  # fmt: skip
            out, _ = await asyncio.wait_for(proc.communicate(), 30)
        except (OSError, TimeoutError) as exc:
            return False, f"docker unavailable: {exc}"
        if proc.returncode != 0:
            return (
                False,
                f"image {self.image} not found (build infra/docker/blender-worker.Dockerfile)",
            )
        return True, f"{self.image} {out.decode().strip()[:19]}"


__all__ = ["DEFAULT_IMAGE", "ContainerJobRunner"]
