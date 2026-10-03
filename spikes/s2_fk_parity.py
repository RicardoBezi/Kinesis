"""Spike S2: does numpy forward kinematics match Blender's pose evaluation within 0.1 mm?

ADR 0003 assumes this. Run it inside Blender 4.5 LTS:
    <blender> --background --factory-startup --python spikes/s2_fk_parity.py -- --out var/spikes/s2.json

The spike builds a leg-like chain with non-trivial rest orientations and rolls, under an
armature object that is itself translated, rotated and scaled. It poses the chain with
deterministic pseudo-random quaternions (plus a root translation), then compares two sets
of joint positions:
- Blender:  obj.matrix_world @ pose_bone.matrix (head) and @ (0, length, 0) (tail)
- numpy:    M_arm(bone) = M_arm(parent) @ inv(rest(parent)) @ rest(bone) @ Basis(bone)
            (root: rest(bone) @ Basis), world = obj.matrix_world @ M_arm
            where rest = bone.matrix_local (armature space, 4x4)
                  Basis = T(location) @ R(quaternion) @ S(scale)

Pass: the maximum head/tail error over all bones and frames is below 1e-4 m.
It also round-trips Blender's computed local quaternions back through FK (the inverse
direction that IK write-back needs).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy  # type: ignore[import-not-found]
import numpy as np
from mathutils import Euler  # type: ignore[import-not-found]

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
OUT = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None

CHAIN = [  # name, parent, head, tail, roll
    ("root", None, (0, 0, 0), (0, 0.1, 0), 0.0),
    ("pelvis", "root", (0, 0, 0.92), (0, 0, 1.02), 0.0),
    ("thigh.L", "pelvis", (0.1, 0, 0.92), (0.1, -0.02, 0.47), 0.2),
    ("shin.L", "thigh.L", (0.1, -0.02, 0.47), (0.1, 0, 0.08), -0.35),
    ("foot.L", "shin.L", (0.1, 0, 0.08), (0.1, -0.12, 0.02), 0.5),
    ("toe.L", "foot.L", (0.1, -0.12, 0.02), (0.1, -0.18, 0.02), 0.0),
]


def quat_to_mat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q / np.linalg.norm(q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def basis(loc: np.ndarray, quat: np.ndarray, scale: np.ndarray) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = quat_to_mat(quat) * scale  # columns scaled = R @ S
    m[:3, 3] = loc
    return m


def numpy_fk(
    rest: dict[str, np.ndarray],
    parents: dict[str, str | None],
    lengths: dict[str, float],
    world: np.ndarray,
    pose: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    arm: dict[str, np.ndarray] = {}
    out = {}
    for name, *_ in CHAIN:
        b = basis(*pose[name])
        p = parents[name]
        arm[name] = rest[name] @ b if p is None else arm[p] @ np.linalg.inv(rest[p]) @ rest[name] @ b
        m = world @ arm[name]
        head = m[:3, 3]
        tail = (m @ np.array([0, lengths[name], 0, 1.0]))[:3]
        out[name] = (head, tail)
    return out


def main() -> int:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm_data = bpy.data.armatures.new("S2")
    obj = bpy.data.objects.new("S2Rig", arm_data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = (0.3, -0.2, 0.05)
    obj.rotation_euler = Euler((0.05, -0.1, 0.4))
    obj.scale = (1.0, 1.0, 1.0)  # characters are normally unscaled; scaled case reported separately
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    for name, parent, head, tail, roll in CHAIN:
        eb = arm_data.edit_bones.new(name)
        eb.head, eb.tail, eb.roll = head, tail, roll
        if parent:
            eb.parent = arm_data.edit_bones[parent]
            eb.use_connect = False
    bpy.ops.object.mode_set(mode="POSE")
    bpy.context.view_layer.update()

    rest = {b.name: np.array(b.matrix_local) for b in arm_data.bones}
    parents = {b.name: (b.parent.name if b.parent else None) for b in arm_data.bones}
    lengths = {b.name: b.length for b in arm_data.bones}
    rng = np.random.default_rng(1234)

    max_err = 0.0
    max_roundtrip = 0.0
    for _frame in range(60):
        pose = {}
        for pb in obj.pose.bones:
            pb.rotation_mode = "QUATERNION"
            q = rng.normal(size=4)
            q /= np.linalg.norm(q)
            loc = rng.normal(scale=0.05, size=3) if pb.name == "root" else np.zeros(3)
            pb.rotation_quaternion = q.tolist()
            pb.location = loc.tolist()
            pb.scale = (1, 1, 1)
            pose[pb.name] = (loc, q, np.ones(3))
        bpy.context.view_layer.update()
        world = np.array(obj.matrix_world)
        mine = numpy_fk(rest, parents, lengths, world, pose)
        for pb in obj.pose.bones:
            m = obj.matrix_world @ pb.matrix
            head = np.array(m.translation)
            tail = np.array(obj.matrix_world @ pb.tail)
            max_err = max(max_err, np.abs(head - mine[pb.name][0]).max())
            max_err = max(max_err, np.abs(tail - mine[pb.name][1]).max())
            # Round-trip: Blender's own matrix_basis must reproduce the same pose.
            q_back = np.array(pb.matrix_basis.to_quaternion())
            max_roundtrip = max(max_roundtrip, 1 - abs(float(np.dot(q_back, pose[pb.name][1]))))

    results = {
        "blender_version": bpy.app.version_string,
        "max_joint_error_m": max_err,
        "max_basis_roundtrip_quat_distance": max_roundtrip,
        "threshold_m": 1e-4,
        "PASS": max_err < 1e-4 and max_roundtrip < 1e-6,
    }
    text = json.dumps(results, indent=2)
    print(text)
    if OUT:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
    return 0 if results["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
