"""LocalJobRunner: runs the Blender worker as a headless subprocess (ADR 0003).

The argv is fixed (blender/worker/io_contract.md); the only variable parts are the command
enum and paths the backend created inside the job directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from pathlib import Path

from kinesis.errors import WorkerCrashed, WorkerTimeout
from kinesis.jobs.runner import WorkerInvocation, WorkerOutcome, assert_inside

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKER_SCRIPT = REPO_ROOT / "blender" / "worker" / "main.py"
SCENE_RELPATH = "input/scene.blend"
HANDLED_FAILURE_EXIT = 2  # the worker caught an exception and wrote a WorkerFailure
CRASH_EXIT = 3  # --python-exit-code: an exception escaped the worker


class LocalJobRunner:
    name = "local"

    def __init__(self, blender_bin: Path, worker_script: Path = WORKER_SCRIPT) -> None:
        self.blender_bin = blender_bin
        self.worker_script = worker_script

    def argv(self, invocation: WorkerInvocation) -> list[str]:
        job_dir = invocation.job_dir
        return [
            str(self.blender_bin),
            "--background",
            "--factory-startup",
            "-noaudio",
            str(assert_inside(job_dir, SCENE_RELPATH)),
            "--python-exit-code",
            str(CRASH_EXIT),
            "--python",
            str(self.worker_script),
            "--",
            "--command",
            invocation.command.value,
            "--spec",
            str(assert_inside(job_dir, invocation.spec_relpath)),
        ]

    async def run(self, invocation: WorkerInvocation) -> WorkerOutcome:
        job_dir = invocation.job_dir
        argv = self.argv(invocation)
        result_path = assert_inside(job_dir, invocation.result_relpath)
        log_relpath = str(Path(invocation.spec_relpath).with_suffix("").with_suffix(".log"))
        log_relpath = log_relpath.replace("\\", "/")
        log_path = assert_inside(job_dir, log_relpath)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.unlink(missing_ok=True)  # never mistake a stale result for a fresh one

        started = time.monotonic()
        with log_path.open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd=job_dir
            )
            try:
                exit_code = await asyncio.wait_for(proc.wait(), invocation.timeout_s)
            except asyncio.CancelledError:  # job cancelled: never leave Blender running
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await asyncio.shield(proc.wait())
                raise
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
                raise WorkerTimeout(
                    f"{invocation.command.value} exceeded {invocation.timeout_s:.0f} s"
                ) from None
        elapsed_ms = int((time.monotonic() - started) * 1000)

        handled = exit_code == HANDLED_FAILURE_EXIT and result_path.exists()
        if exit_code != 0 and not handled:
            raise WorkerCrashed(
                f"{invocation.command.value} exited with {exit_code}; see {log_relpath}"
            )
        return WorkerOutcome(
            result_path=result_path,
            elapsed_ms=elapsed_ms,
            exit_code=exit_code,
            log_relpath=log_relpath,
        )

    async def health_check(self) -> tuple[bool, str]:
        if not self.blender_bin.exists():
            return False, f"blender not found at {self.blender_bin}"
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.blender_bin),
                "--background",
                "--factory-startup",
                "--version",
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 60)
        except (OSError, TimeoutError) as exc:
            return False, f"blender failed to start: {exc}"
        first = out.decode(errors="replace").strip().splitlines()[:1]
        return proc.returncode == 0, first[0] if first else "no version output"


__all__ = ["WORKER_SCRIPT", "LocalJobRunner"]
