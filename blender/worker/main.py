"""Kinesis Blender worker entry point (see io_contract.md).

It runs inside Blender with ``--python blender/worker/main.py -- --command <c> --spec <path>``.
It uses only bpy, mathutils and Blender's bundled numpy, and never pip-installed packages.

``inspect`` and ``extract`` are implemented (Phase 1). ``apply_render`` and ``export`` follow in
Phase 2. Every handler returns a plain dict that the backend validates against
``kinesis.schemas.worker``.
"""

from __future__ import annotations

import hashlib
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


# --------------------------------------------------------------------------- helpers


def _bpy() -> Any:
    import bpy  # type: ignore[import-not-found]  # only importable inside Blender

    return bpy


def _matrix(m: Any) -> list[list[float]]:
    return [[float(m[r][c]) for c in range(4)] for r in range(4)]


def _vec(v: Any) -> list[float]:
    return [float(v[0]), float(v[1]), float(v[2])]


def _armature(name: str) -> Any:
    obj = _bpy().data.objects.get(name)
    if obj is None or obj.type != "ARMATURE":
        raise LookupError(f"armature {name!r} not found")
    return obj


def action_fcurves(action: Any) -> list[Any]:
    """All F-curves of an action, from the slotted layout (4.4+) or the legacy list."""
    fcurves: list[Any] = []
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            for bag in getattr(strip, "channelbags", ()):
                fcurves.extend(bag.fcurves)
    if not fcurves and hasattr(action, "fcurves"):
        fcurves = list(action.fcurves)
    return fcurves


def action_hash(action: Any) -> str:
    """Content hash of an action's keys: data paths, frames, values and interpolation.

    Used to prove that a repair never modified the original action (ADR 0004, spike S1).
    """
    h = hashlib.sha256()
    if action is None:
        return h.hexdigest()
    for fc in sorted(action_fcurves(action), key=lambda c: (c.data_path, c.array_index)):
        h.update(f"{fc.data_path}[{fc.array_index}]".encode())
        for kp in fc.keyframe_points:
            h.update(f"{kp.co[0]:.6f},{kp.co[1]:.9f},{kp.interpolation};".encode())
    return h.hexdigest()


def _scene_range() -> tuple[int, int, float]:
    scene = _bpy().context.scene
    return int(scene.frame_start), int(scene.frame_end), scene.render.fps / scene.render.fps_base


# --------------------------------------------------------------------------- commands


def inspect(spec: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    bpy = _bpy()
    start, end, fps = _scene_range()
    armatures = [
        {
            "name": obj.name,
            "bones": [
                {
                    "name": b.name,
                    "parent": b.parent.name if b.parent else None,
                    "deform": bool(b.use_deform),
                }
                for b in obj.data.bones
            ],
        }
        for obj in sorted(bpy.data.objects, key=lambda o: o.name)
        if obj.type == "ARMATURE"
    ]
    return {
        "protocol": PROTOCOL,
        "ok": True,
        "blender_version": bpy.app.version_string,
        "frame_start": start,
        "frame_end": end,
        "fps": fps,
        "armatures": armatures,
        "autoexec_disabled": not bpy.context.preferences.filepaths.use_scripts_auto_execute,
    }


def extract(spec: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    """Chain matrices over the context window, plus every deform bone over the whole scene."""
    bpy = _bpy()
    scene = bpy.context.scene
    obj = _armature(spec["armature"])
    bones = obj.data.bones
    chain = list(spec["chain_bones"])
    missing = [b for b in chain if b not in bones]
    if missing:
        raise LookupError(f"bones not found: {missing}")
    scene_start, scene_end, fps = _scene_range()
    ctx_start, ctx_end = int(spec["frame_start"]), int(spec["frame_end"])
    if not scene_start <= ctx_start <= ctx_end <= scene_end:
        raise ValueError(f"context {ctx_start}-{ctx_end} outside scene {scene_start}-{scene_end}")

    deform = [b.name for b in bones if b.use_deform]
    chain_world: dict[str, list[list[list[float]]]] = {b: [] for b in chain}
    chain_local: dict[str, list[list[float]]] = {b: [] for b in chain}
    heads: dict[str, list[list[float]]] = {b: [] for b in deform}
    tails: dict[str, list[list[float]]] = {b: [] for b in deform}
    original_frame = scene.frame_current
    try:
        for f in range(scene_start, scene_end + 1):
            scene.frame_set(f)
            world = obj.matrix_world
            for name in deform:
                pb = obj.pose.bones[name]
                heads[name].append(_vec(world @ pb.head))
                tails[name].append(_vec(world @ pb.tail))
            if ctx_start <= f <= ctx_end:
                for name in chain:
                    pb = obj.pose.bones[name]
                    chain_world[name].append(_matrix(world @ pb.matrix))
                    q = pb.matrix_basis.to_quaternion()
                    chain_local[name].append([float(q.w), float(q.x), float(q.y), float(q.z)])
    finally:
        scene.frame_set(original_frame)

    action = obj.animation_data.action if obj.animation_data else None
    return {
        "protocol": PROTOCOL,
        "ok": True,
        "armature_world": _matrix(obj.matrix_world),
        "rest": [
            {
                "name": b.name,
                "parent": b.parent.name if b.parent else None,
                "matrix_local": _matrix(b.matrix_local),
                "length": float(b.length),
            }
            for b in bones
        ],
        "scene_frame_start": scene_start,
        "scene_frame_end": scene_end,
        "fps": fps,
        "context_frame_start": ctx_start,
        "chain": [
            {"name": b, "world": chain_world[b], "pose_local": chain_local[b]} for b in chain
        ],
        "all_bones": [{"name": b, "head": heads[b], "tail": tails[b]} for b in deform],
        "original_action_hash": action_hash(action),
    }


def _not_implemented(phase: str) -> Callable[[dict[str, Any], Path], dict[str, Any]]:
    def handler(spec: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        raise NotImplementedError(f"worker command planned for {phase}")

    return handler


HANDLERS: dict[str, Callable[[dict[str, Any], Path], dict[str, Any]]] = {
    "inspect": inspect,
    "extract": extract,
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
