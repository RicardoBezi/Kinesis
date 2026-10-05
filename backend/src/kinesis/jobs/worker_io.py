"""Writing worker specs and validating worker results (blender/worker/io_contract.md).

The worker runs without pydantic, so the backend is the only place its output is validated.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from kinesis.errors import WorkerOutputInvalid, WorkerReportedFailure
from kinesis.jobs.runner import JobRunner, WorkerInvocation, assert_inside
from kinesis.schemas.common import KinesisModel
from kinesis.schemas.worker import WorkerCommand, WorkerFailure

MAX_RESULT_BYTES = 256 * 1024 * 1024


def spec_paths(node: str) -> tuple[str, str]:
    """Job-relative ``(spec, result)`` paths for a DAG node."""
    if not node.replace("_", "").isalnum():
        raise ValueError(f"invalid node name {node!r}")
    return f"work/{node}.spec.json", f"work/{node}.result.json"


def write_spec(job_dir: Path, spec_relpath: str, spec: KinesisModel) -> Path:
    path = assert_inside(job_dir, spec_relpath)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    return path


def read_result[M: KinesisModel](path: Path, model: type[M]) -> M:
    """Parse ``result.json`` as ``model``.

    Raises ``WorkerReportedFailure`` when the worker wrote a ``WorkerFailure`` and
    ``WorkerOutputInvalid`` when the file is missing, oversized or does not validate.
    """
    try:
        if path.stat().st_size > MAX_RESULT_BYTES:
            raise WorkerOutputInvalid(f"{path.name} exceeds {MAX_RESULT_BYTES} bytes")
        raw = path.read_bytes()
    except FileNotFoundError:
        raise WorkerOutputInvalid(f"{path.name} was not written") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise WorkerOutputInvalid(f"{path.name} is not JSON: {exc}") from None
    if isinstance(data, dict) and data.get("ok") is False:
        try:
            failure = WorkerFailure.model_validate(data)
        except ValidationError as exc:
            raise WorkerOutputInvalid(f"malformed WorkerFailure: {exc}") from None
        raise WorkerReportedFailure(failure.error_type, failure.message)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise WorkerOutputInvalid(
            f"{path.name} does not match {model.__name__}: {exc.error_count()} errors; "
            f"first: {exc.errors()[0]['msg']} at {exc.errors()[0]['loc']}"
        ) from None


async def run_worker[M: KinesisModel](
    runner: JobRunner,
    job_dir: Path,
    node: str,
    command: WorkerCommand,
    spec: KinesisModel,
    result_model: type[M],
    timeout_s: float,
) -> M:
    """Write the spec, run the worker, and return the validated result."""
    spec_rel, result_rel = spec_paths(node)
    declared = getattr(spec, "result_path", None)
    if declared != result_rel:
        raise ValueError(f"spec.result_path must be {result_rel!r}, got {declared!r}")
    write_spec(job_dir, spec_rel, spec)
    outcome = await runner.run(
        WorkerInvocation(
            job_dir=job_dir,
            command=command,
            spec_relpath=spec_rel,
            result_relpath=result_rel,
            timeout_s=timeout_s,
        )
    )
    return read_result(outcome.result_path, result_model)


__all__ = ["read_result", "run_worker", "spec_paths", "write_spec"]
