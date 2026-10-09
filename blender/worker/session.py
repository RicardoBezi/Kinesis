"""Session worker for design B (one Nebius job per Kinesis job). Runs in the worker image with
Blender's bundled Python (standard library only):

    /opt/blender/4.5/python/bin/python3.11 /opt/kinesis/worker/session.py --idle-timeout 600

It watches ``/work/queue/`` (the job's bucket prefix, mounted read-write) for
``<id>.request.json`` files: ``{"command": "<enum>", "spec": "/work/work/<node>.spec.json"}``.
Each request runs the worker with exactly the same fixed argv as a single-step job, then
``<id>.done.json`` (``{"exit_code": n}``) is written. Requests are processed in name order, one
at a time. The loop ends when ``queue/STOP`` appears or nothing arrives for ``--idle-timeout``
seconds, so a forgotten session cannot run (and bill) for long.

Files are written directly, never renamed, because S3-backed mounts may not support renames.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

BLENDER = "/opt/blender/blender"
WORKER = "/opt/kinesis/worker/main.py"
COMMANDS = ("inspect", "extract", "apply_render", "export")


def worker_argv(command: str, spec: str, work: Path) -> list[str]:
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    spec_path = Path(spec)
    if not spec_path.is_absolute() or work not in spec_path.parents:
        raise ValueError(f"spec outside {work}: {spec!r}")
    return [
        BLENDER, "--background", "--factory-startup", "-noaudio", str(work / "input" / "scene.blend"),
        "--python-exit-code", "3", "--python", WORKER, "--", "--command", command, "--spec", spec,
    ]  # fmt: skip


def handle(request: Path, work: Path, run: object = subprocess.run) -> int:
    try:
        data = json.loads(request.read_text(encoding="utf-8"))
        argv = worker_argv(str(data["command"]), str(data["spec"]), work)
    except (OSError, ValueError, KeyError) as exc:
        print(f"bad request {request.name}: {exc}", flush=True)
        return 2
    log = work / "work" / f"{request.name.removesuffix('.request.json')}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as out:
        completed = run(argv, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, check=False)  # type: ignore[operator]
    return int(completed.returncode)


def main(argv: list[str]) -> int:
    idle_timeout = float(argv[argv.index("--idle-timeout") + 1]) if "--idle-timeout" in argv else 600.0
    work = Path("/work")
    queue = work / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    last_activity = time.monotonic()
    while True:
        if (queue / "STOP").exists():
            print("STOP received", flush=True)
            return 0
        requests = sorted(
            p for p in queue.glob("*.request.json")
            if not (queue / p.name.replace(".request.json", ".done.json")).exists()
        )  # fmt: skip
        if not requests:
            if time.monotonic() - last_activity > idle_timeout:
                print(f"idle for {idle_timeout:.0f} s; exiting", flush=True)
                return 0
            time.sleep(1.0)
            continue
        request = requests[0]
        code = handle(request, work)
        done = queue / request.name.replace(".request.json", ".done.json")
        done.write_text(json.dumps({"exit_code": code}), encoding="utf-8")
        last_activity = time.monotonic()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
