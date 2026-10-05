"""Scope resolution: temporal and skeletal cropping (docs/ALGORITHMS.md §1)."""

from __future__ import annotations

from collections.abc import Mapping

from kinesis.errors import SelectionInvalid
from kinesis.schemas.common import ErrorCode
from kinesis.schemas.scope import MAX_CONTEXT_FRAMES, SkeletalScope, TemporalScope


def resolve_temporal(temporal: TemporalScope, scene_range: tuple[int, int]) -> tuple[int, int]:
    """§1.1: the context window clipped to the scene, after checking the request fits it."""
    scene_start, scene_end = scene_range
    if temporal.frame_start < scene_start or temporal.frame_end > scene_end:
        raise SelectionInvalid(
            ErrorCode.INVALID_FRAME_RANGE,
            f"frames {temporal.frame_start}-{temporal.frame_end} are outside the scene range "
            f"{scene_start}-{scene_end}",
        )
    start, end = temporal.clipped(scene_start, scene_end)
    if end - start + 1 > MAX_CONTEXT_FRAMES:  # unreachable after schema validation; kept as a guard
        raise SelectionInvalid(
            ErrorCode.INVALID_FRAME_RANGE, f"context window exceeds {MAX_CONTEXT_FRAMES} frames"
        )
    return start, end


def _ancestors(bone: str, parents: Mapping[str, str | None]) -> list[str]:
    out: list[str] = []
    current = parents[bone]
    while current is not None:
        if current in out or len(out) > len(parents):
            raise SelectionInvalid(ErrorCode.UNSUPPORTED_RIG, "bone hierarchy contains a cycle")
        out.append(current)
        current = parents[current]
    return out


def resolve_skeletal(
    armature: str, target_bones: tuple[str, ...], parents: Mapping[str, str | None]
) -> SkeletalScope:
    """§1.2: map each target foot to its thigh -> shin -> foot chain.

    ``parents`` maps every bone of the armature to its parent (``None`` for roots).

    - chain: thigh, shin, the target, and the target's direct children (the toe);
    - keyable: thigh, shin and the target (children follow their parent and are not keyed);
    - context: the thigh's ancestors up to the root (read-only; hip position, root motion).
    """
    chain: list[str] = []
    keyable: list[str] = []
    context: list[str] = []
    for target in target_bones:
        if target not in parents:
            raise SelectionInvalid(
                ErrorCode.BONE_NOT_FOUND, f"bone {target!r} is not in armature {armature!r}"
            )
        ancestors = _ancestors(target, parents)
        if set(ancestors) & set(target_bones):
            raise SelectionInvalid(
                ErrorCode.UNSUPPORTED_RIG, "target bones must not be ancestors of each other"
            )
        if len(ancestors) < 2:
            raise SelectionInvalid(
                ErrorCode.UNSUPPORTED_RIG,
                f"{target!r} needs a thigh -> shin -> foot chain (found {len(ancestors)} parent"
                f"{'' if len(ancestors) == 1 else 's'})",
            )
        shin, thigh = ancestors[0], ancestors[1]
        children = sorted(b for b, p in parents.items() if p == target)
        for bone in (thigh, shin, target, *children):
            if bone not in chain:
                chain.append(bone)
        for bone in (thigh, shin, target):
            if bone not in keyable:
                keyable.append(bone)
        for bone in ancestors[2:]:
            if bone not in context:
                context.append(bone)
    context = [b for b in context if b not in chain]
    return SkeletalScope(
        armature=armature,
        target_bones=target_bones,
        chain_bones=tuple(chain),
        keyable_bones=tuple(keyable),
        context_bones=tuple(context),
    )


__all__ = ["resolve_skeletal", "resolve_temporal"]
