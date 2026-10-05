"""Turn a validated worker ``ExtractResult`` into numpy arrays and run detection on them."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from kinesis.analysis.detection import ContactPlane, Detection, detect_foot_slide
from kinesis.analysis.kinematics import Array, Skeleton
from kinesis.analysis.scope import resolve_temporal
from kinesis.errors import WorkerOutputInvalid
from kinesis.schemas.scope import AnimationSelection, SkeletalScope
from kinesis.schemas.worker import ExtractResult


@dataclass(frozen=True)
class ExtractedMotion:
    """Arrays from one ``extract`` run.

    - ``chain_world``: ``(T_ctx, 4, 4)`` per chain bone over the context window;
    - ``chain_local``: ``(T_ctx, 4)`` pose quaternions per chain bone;
    - ``heads`` / ``tails``: ``(T_scene, 3)`` per deform bone over the whole scene.
    """

    skeleton: Skeleton
    armature_world: Array
    fps: float
    scene_range: tuple[int, int]
    context_range: tuple[int, int]
    chain_world: dict[str, Array]
    chain_local: dict[str, Array]
    heads: dict[str, Array]
    tails: dict[str, Array]
    original_action_hash: str

    def context_slice(self) -> slice:
        """Index range of the context window inside the whole-scene arrays."""
        offset = self.context_range[0] - self.scene_range[0]
        return slice(offset, offset + self.context_range[1] - self.context_range[0] + 1)


def _ordered_skeleton(result: ExtractResult) -> Skeleton:
    """Skeleton with parents before children (Blender's bone order is not guaranteed)."""
    by_name = {b.name: b for b in result.rest}
    order: list[str] = []
    placed: set[str] = set()
    pending = list(by_name)
    while pending:
        progressed = False
        for name in list(pending):
            parent = by_name[name].parent
            if parent is None or parent in placed:
                order.append(name)
                placed.add(name)
                pending.remove(name)
                progressed = True
        if not progressed:
            raise WorkerOutputInvalid(f"bone hierarchy is cyclic or dangling: {pending[:5]}")
    return Skeleton(
        names=tuple(order),
        parents=tuple(by_name[n].parent for n in order),
        rest={n: np.array(by_name[n].matrix_local, dtype=np.float64) for n in order},
        lengths={n: by_name[n].length for n in order},
    )


def motion_from_extract(result: ExtractResult) -> ExtractedMotion:
    scene_len = result.scene_frame_end - result.scene_frame_start + 1
    ctx_lengths = {len(c.world) for c in result.chain} | {len(c.pose_local) for c in result.chain}
    if len(ctx_lengths) > 1:
        raise WorkerOutputInvalid("chain arrays have inconsistent lengths")
    ctx_len = ctx_lengths.pop() if ctx_lengths else 0
    ctx_end = result.context_frame_start + ctx_len - 1
    if ctx_len < 2 or not (
        result.scene_frame_start <= result.context_frame_start <= ctx_end <= result.scene_frame_end
    ):
        raise WorkerOutputInvalid("context window is empty or outside the scene range")
    for bone in result.all_bones:
        if len(bone.head) != scene_len or len(bone.tail) != scene_len:
            raise WorkerOutputInvalid(f"{bone.name}: expected {scene_len} samples")
    return ExtractedMotion(
        skeleton=_ordered_skeleton(result),
        armature_world=np.array(result.armature_world, dtype=np.float64),
        fps=result.fps,
        scene_range=(result.scene_frame_start, result.scene_frame_end),
        context_range=(result.context_frame_start, ctx_end),
        chain_world={c.name: np.array(c.world, dtype=np.float64) for c in result.chain},
        chain_local={c.name: np.array(c.pose_local, dtype=np.float64) for c in result.chain},
        heads={b.name: np.array(b.head, dtype=np.float64) for b in result.all_bones},
        tails={b.name: np.array(b.tail, dtype=np.float64) for b in result.all_bones},
        original_action_hash=result.original_action_hash,
    )


def foot_and_toe(motion: ExtractedMotion, foot: str, toe: str | None) -> tuple[Array, Array]:
    """Ankle and ball-of-foot world positions over the context window.

    The ball of the foot is the toe's head when the rig has a toe bone, otherwise the foot's
    tail (the same point on connected rigs).
    """
    if foot not in motion.chain_world:
        raise WorkerOutputInvalid(f"{foot!r} missing from the extracted chain")
    world = motion.chain_world[foot]
    ankle: Array = world[:, :3, 3]
    if toe is not None and toe in motion.chain_world:
        ball: Array = motion.chain_world[toe][:, :3, 3]
    else:
        ball = ankle + world[:, :3, 1] * motion.skeleton.lengths[foot]
    return ankle, ball


def detect_from_extract(
    selection: AnimationSelection, scope: SkeletalScope, result: ExtractResult
) -> tuple[ExtractedMotion, Detection]:
    """Phase 1 analysis: extract output -> per-frame features -> defect report.

    The MVP repairs one foot; the first target bone is analysed.
    """
    motion = motion_from_extract(result)
    expected = resolve_temporal(selection.temporal, motion.scene_range)
    if expected != motion.context_range:
        raise WorkerOutputInvalid(
            f"extract covered {motion.context_range}, the selection needs {expected}"
        )
    foot = scope.target_bones[0]
    children = [b for b in scope.chain_bones if motion.skeleton.parent_of(b) == foot]
    ankle, ball = foot_and_toe(motion, foot, children[0] if children else None)
    contact = selection.contact
    detection = detect_foot_slide(
        ankle,
        ball,
        frame_start=motion.context_range[0],
        fps=motion.fps,
        selection=(selection.temporal.frame_start, selection.temporal.frame_end),
        plane=ContactPlane(origin=contact.plane_point, normal=contact.plane_normal),
        bone=foot,
    )
    return motion, detection


__all__ = ["ExtractedMotion", "detect_from_extract", "foot_and_toe", "motion_from_extract"]
