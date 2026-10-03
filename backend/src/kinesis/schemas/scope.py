"""Selection and cropping contracts (docs/ALGORITHMS.md §1)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from kinesis.schemas.common import BlenderName, Frame, Identifier, KinesisModel, Vec3

MAX_CONTEXT_FRAMES = 600
DEFAULT_CONTEXT_FRAMES = 10


class TemporalScope(KinesisModel):
    """The requested frame range plus context padding.

    ``context_start`` and ``context_end`` are derived properties, not serialized fields.
    They are not clipped here. The server clips them to the scene range
    when it resolves the scope.
    """

    frame_start: Frame
    frame_end: Frame
    context_before: int = Field(default=DEFAULT_CONTEXT_FRAMES, ge=0, le=120)
    context_after: int = Field(default=DEFAULT_CONTEXT_FRAMES, ge=0, le=120)

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.frame_end < self.frame_start:
            raise ValueError("frame_end must be >= frame_start")
        if self.context_end - self.context_start + 1 > MAX_CONTEXT_FRAMES:
            raise ValueError(f"context window exceeds {MAX_CONTEXT_FRAMES} frames")
        return self

    @property
    def context_start(self) -> int:
        return self.frame_start - self.context_before

    @property
    def context_end(self) -> int:
        return self.frame_end + self.context_after

    def clipped(self, scene_start: int, scene_end: int) -> tuple[int, int]:
        """Return the context window clipped to the scene range."""
        return max(scene_start, self.context_start), min(scene_end, self.context_end)


class SkeletalScope(KinesisModel):
    """The bone sets the repair works on, resolved by the server from the armature hierarchy.

    - ``chain_bones``: the bones whose motion may change, for example thigh, shin, foot, toe.
    - ``keyable_bones``: the subset of the chain that actually receives keys.
    - ``context_bones``: read-only ancestors, used for the hip position and root motion.
    """

    armature: BlenderName
    target_bones: tuple[BlenderName, ...] = Field(min_length=1, max_length=8)
    chain_bones: tuple[BlenderName, ...] = Field(min_length=1, max_length=16)
    keyable_bones: tuple[BlenderName, ...] = Field(min_length=1, max_length=16)
    context_bones: tuple[BlenderName, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _check_subsets(self) -> Self:
        chain = set(self.chain_bones)
        if not set(self.target_bones) <= chain:
            raise ValueError("target_bones must be a subset of chain_bones")
        if not set(self.keyable_bones) <= chain:
            raise ValueError("keyable_bones must be a subset of chain_bones")
        if chain & set(self.context_bones):
            raise ValueError("context_bones must not overlap chain_bones")
        return self


class ContactKind(StrEnum):
    FLOOR_PLANE = "FLOOR_PLANE"
    OBJECT = "OBJECT"  # reserved; the MVP measures against the object's top plane


class ContactTarget(KinesisModel):
    kind: ContactKind = ContactKind.FLOOR_PLANE
    object_name: BlenderName | None = None
    plane_point: Vec3 = (0.0, 0.0, 0.0)
    plane_normal: Vec3 = (0.0, 0.0, 1.0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind is ContactKind.OBJECT and self.object_name is None:
            raise ValueError("object_name is required when kind is OBJECT")
        nx, ny, nz = self.plane_normal
        norm = (nx * nx + ny * ny + nz * nz) ** 0.5
        if abs(norm - 1.0) > 1e-6:
            raise ValueError("plane_normal must be a unit vector")
        return self


class AnimationSelection(KinesisModel):
    """What the animator selected in Blender."""

    scene_id: Identifier
    armature: BlenderName
    target_bones: tuple[BlenderName, ...] = Field(min_length=1, max_length=8)
    temporal: TemporalScope
    contact: ContactTarget = ContactTarget()
    instruction: Annotated[str, StringConstraints(max_length=500, strip_whitespace=True)] | None = (
        None
    )

    @model_validator(mode="after")
    def _unique_bones(self) -> Self:
        if len(set(self.target_bones)) != len(self.target_bones):
            raise ValueError("target_bones must be unique")
        return self
