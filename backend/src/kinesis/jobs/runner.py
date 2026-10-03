"""JobRunner: runs one Blender worker command for one job (ADR 0003, ARCHITECTURE §5).

LocalJobRunner (Phase 1) spawns a headless Blender subprocess; NebiusJobRunner (Phase 6)
submits the same command to Nebius Serverless Jobs. Both share this interface and the
same spec/result files, so the orchestrator and tests cannot tell them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from kinesis.schemas.worker import WorkerCommand


@dataclass(frozen=True, slots=True)
class WorkerInvocation:
    job_dir: Path  # absolute; every path the worker touches is inside it
    command: WorkerCommand
    spec_relpath: str  # e.g. "work/extract_scope.spec.json"
    result_relpath: str  # e.g. "work/extract_scope.result.json"
    timeout_s: float


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    result_path: Path
    elapsed_ms: int
    exit_code: int
    log_relpath: str | None = None  # captured stdout/stderr, stored as a LOG artifact


@runtime_checkable
class JobRunner(Protocol):
    """Contract:
    - argv is fixed: the only variable parts are the enum ``command`` and paths the backend
      created inside ``job_dir`` (never client-provided strings);
    - raises ``WorkerTimeout`` / ``WorkerCrashed`` for process failures; result parsing and
      ``WorkerOutputInvalid`` are the caller's job (it owns the expected schema);
    - must be safe to call concurrently for different invocations.
    """

    name: str

    async def run(self, invocation: WorkerInvocation) -> WorkerOutcome: ...

    async def health_check(self) -> tuple[bool, str]: ...


def assert_inside(root: Path, relpath: str) -> Path:
    """Resolve ``relpath`` under ``root`` and refuse anything that escapes it."""
    if not relpath or relpath.startswith(("/", "\\")) or ".." in Path(relpath).parts:
        raise ValueError(f"unsafe relative path: {relpath!r}")
    resolved = (root / relpath).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes job directory: {relpath!r}")
    return resolved
