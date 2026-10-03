"""Kinesis Blender worker entry point (see io_contract.md).

It runs inside Blender with ``--python blender/worker/main.py -- --command <c> --spec <path>``.
It uses only bpy, mathutils and Blender's bundled numpy, and never pip-installed packages.

Phase 0 is a stub: it validates the arguments and dispatches. The commands are implemented
in Phase 1 (inspect, extract) and Phase 2 (apply_render, export).
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROTOCOL = 1
COMMANDS = ("inspect", "extract", "apply_render", "export")


def _args() -> tuple[str, Path]:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if "--command" not in argv or "--spec" not in argv:
        raise SystemExit("usage: ... -- --command <cmd> --spec <spec.json>")
    command = argv[argv.index("--command") + 1]
    if command not in COMMANDS:
        raise SystemExit(f"unknown command {command!r}")
    return command, Path(argv[argv.index("--spec") + 1]).resolve()


def job_path(job_dir: Path, rel: str) -> Path:
    """Resolve a spec-relative path, refusing anything that escapes the job directory."""
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise ValueError(f"unsafe path in spec: {rel!r}")
    p = (job_dir / rel).resolve()
    if job_dir.resolve() not in p.parents and p != job_dir.resolve():
        raise ValueError(f"path escapes job dir: {rel!r}")
    return p


def _not_implemented(phase: str) -> Callable[[dict[str, Any], Path], dict[str, Any]]:
    def handler(spec: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        raise NotImplementedError(f"worker command planned for {phase}")

    return handler


HANDLERS: dict[str, Callable[[dict[str, Any], Path], dict[str, Any]]] = {
    "inspect": _not_implemented("Phase 1"),
    "extract": _not_implemented("Phase 1"),
    "apply_render": _not_implemented("Phase 2"),
    "export": _not_implemented("Phase 2"),
}


def main() -> int:
    command, spec_path = _args()
    job_dir = spec_path.parent.parent  # <job_dir>/work/<node>.spec.json
    spec: dict[str, Any] = json.loads(spec_path.read_text(encoding="utf-8"))
    result_path = job_path(job_dir, spec["result_path"])
    try:
        if spec.get("protocol") != PROTOCOL or spec.get("command") != command:
            raise ValueError("spec protocol/command mismatch")
        result = HANDLERS[command](spec, job_dir)
        code = 0
    except Exception as exc:  # reported to the backend as a WorkerFailure
        result = {
            "protocol": PROTOCOL,
            "ok": False,
            "error_type": type(exc).__name__,
            "message": f"{exc}\n{traceback.format_exc()}"[:4000],
        }
        code = 2
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
