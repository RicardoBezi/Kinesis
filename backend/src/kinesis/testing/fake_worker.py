"""Worker results fabricated from the numpy mirror, for tests that run without Blender.

Kept apart from ``synthetic`` because it needs pydantic, which Blender does not have.
"""

from __future__ import annotations

import hashlib

import numpy as np

from kinesis.analysis.kinematics import (
    Array,
    compose,
    forward_kinematics,
    mat3_to_quat,
    quat_to_mat3,
)
from kinesis.schemas.worker import (
    BoneKeys,
    BoneRest,
    BoneSamples,
    ChainFrames,
    ExtractResult,
    Mat4,
)
from kinesis.testing.synthetic import SyntheticScene


def _mat4(m: Array) -> Mat4:
    r = [tuple(float(v) for v in row) for row in m]
    return (r[0], r[1], r[2], r[3])  # type: ignore[return-value]


def _vecs(a: Array) -> tuple[tuple[float, float, float], ...]:
    return tuple((float(x), float(y), float(z)) for x, y, z in a)


def scene_action_hash(scene: SyntheticScene) -> str:
    """Stand-in for the worker's action hash: stable for identical keys."""
    h = hashlib.sha256()
    for name in scene.skeleton.names:
        h.update(name.encode())
        h.update(np.ascontiguousarray(scene.local_quat[name]).tobytes())
    h.update(np.ascontiguousarray(scene.pelvis_location).tobytes())
    return h.hexdigest()


def extract_result(
    scene: SyntheticScene, chain_bones: tuple[str, ...], context: tuple[int, int]
) -> ExtractResult:
    """What ``extract`` would return for ``scene`` (armature at the world origin)."""
    a, b = context[0] - scene.frame_start, context[1] - scene.frame_start
    skel = scene.skeleton
    return ExtractResult(
        armature_world=_mat4(np.eye(4)),
        rest=tuple(
            BoneRest(
                name=n,
                parent=skel.parent_of(n),
                matrix_local=_mat4(skel.rest[n]),
                length=skel.lengths[n],
            )
            for n in skel.names
        ),
        scene_frame_start=scene.frame_start,
        scene_frame_end=scene.frame_end,
        fps=scene.fps,
        context_frame_start=context[0],
        chain=tuple(
            ChainFrames(
                name=n,
                world=tuple(_mat4(m) for m in scene.world[n][a : b + 1]),
                pose_local=tuple(
                    tuple(float(v) for v in mat3_to_quat(m[:3, :3]))  # type: ignore[misc]
                    for m in scene.basis[n][a : b + 1]
                ),
            )
            for n in chain_bones
        ),
        all_bones=all_bone_samples(scene),
        original_action_hash=scene_action_hash(scene),
    )


def apply_keys(scene: SyntheticScene, keys: tuple[BoneKeys, ...]) -> SyntheticScene:
    """What Blender evaluates with a candidate Replace strip above the original.

    Only the keyed quaternion channels change, and only on keyed frames; every other channel
    (including the pelvis location) falls through from the original, as spike S1 showed.
    """
    basis = {name: m.copy() for name, m in scene.basis.items()}
    for bk in keys:
        for frame, q in zip(bk.frames, bk.rotation_quaternion, strict=True):
            t = frame - scene.frame_start
            basis[bk.bone][t] = compose(quat_to_mat3(q), basis[bk.bone][t][:3, 3])
    world = forward_kinematics(scene.skeleton, basis)
    local = {name: np.array([mat3_to_quat(b[:3, :3]) for b in basis[name]]) for name in basis}
    return SyntheticScene(
        skeleton=scene.skeleton,
        frame_start=scene.frame_start,
        fps=scene.fps,
        basis=basis,
        local_quat=local,
        pelvis_location=scene.pelvis_location,
        world=world,
        armature=scene.armature,
    )


def all_bone_samples(scene: SyntheticScene) -> tuple[BoneSamples, ...]:
    return tuple(
        BoneSamples(name=n, head=_vecs(scene.head(n)), tail=_vecs(scene.tail(n)))
        for n in scene.skeleton.names
    )


__all__ = ["all_bone_samples", "apply_keys", "extract_result", "scene_action_hash"]
