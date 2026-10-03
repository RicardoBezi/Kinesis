"""The Blender worker I/O contract (blender/worker/io_contract.md).

The worker runs inside Blender, which has no pydantic. It reads ``<node>.spec.json``,
written by the backend from these models, and writes ``<node>.result.json``, which the
backend validates against these models. Matrices are 4x4, row-major, and in world space
unless the field name says otherwise.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from kinesis.schemas.api import ArmatureInfo
from kinesis.schemas.common import BlenderName, FiniteFloat, Frame, KinesisModel

WORKER_PROTOCOL_VERSION = 1

Mat4 = tuple[
    tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat],
    tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat],
    tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat],
    tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat],
]
Quat = tuple[FiniteFloat, FiniteFloat, FiniteFloat, FiniteFloat]  # (w, x, y, z), Blender order
RelPath = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./-]{1,200}$")]
"""A path relative to the job directory. ``..`` is rejected by the backend before writing."""


class WorkerCommand(StrEnum):
    INSPECT = "inspect"
    EXTRACT = "extract"
    APPLY_RENDER = "apply_render"
    EXPORT = "export"


# ----------------------------------------------------------------- specs (backend -> worker)


class InspectSpec(KinesisModel):
    protocol: Literal[1] = 1
    command: Literal[WorkerCommand.INSPECT] = WorkerCommand.INSPECT
    result_path: RelPath


class ExtractSpec(KinesisModel):
    protocol: Literal[1] = 1
    command: Literal[WorkerCommand.EXTRACT] = WorkerCommand.EXTRACT
    armature: BlenderName
    chain_bones: tuple[BlenderName, ...]
    frame_start: Frame
    frame_end: Frame
    result_path: RelPath


class BoneKeys(KinesisModel):
    bone: BlenderName
    frames: tuple[Frame, ...]
    rotation_quaternion: tuple[Quat, ...]


class RenderSpec(KinesisModel):
    crop_resolution: int = Field(default=512, ge=64, le=2048)
    crop_frames: tuple[Frame, Frame]
    crop_center: tuple[FiniteFloat, FiniteFloat, FiniteFloat]
    crop_radius_m: float = Field(gt=0, le=20)
    context_every: int = Field(default=4, ge=1, le=60)
    jpeg_quality: int = Field(default=90, ge=10, le=100)
    engine: Literal["BLENDER_WORKBENCH"] = "BLENDER_WORKBENCH"


class ApplyRenderSpec(KinesisModel):
    protocol: Literal[1] = 1
    command: Literal[WorkerCommand.APPLY_RENDER] = WorkerCommand.APPLY_RENDER
    armature: BlenderName
    action_name: BlenderName
    nla_track_name: BlenderName
    keys: tuple[BoneKeys, ...]
    render: RenderSpec | None
    render_original: bool = False
    output_blend: RelPath
    frames_dir: RelPath
    result_path: RelPath


class ExportSpec(KinesisModel):
    protocol: Literal[1] = 1
    command: Literal[WorkerCommand.EXPORT] = WorkerCommand.EXPORT
    candidate_blend: RelPath
    armature: BlenderName
    keep_track: BlenderName
    output_blend: RelPath
    result_path: RelPath


# ----------------------------------------------------------------- results (worker -> backend)


class BoneRest(KinesisModel):
    name: BlenderName
    parent: BlenderName | None
    matrix_local: Mat4  # bone rest matrix in armature space
    length: float = Field(gt=0)


class InspectResult(KinesisModel):
    protocol: Literal[1] = 1
    ok: Literal[True] = True
    blender_version: str
    frame_start: int
    frame_end: int
    fps: float = Field(gt=0)
    armatures: tuple[ArmatureInfo, ...]
    autoexec_disabled: bool


class BoneSamples(KinesisModel):
    """Per-frame world-space head and tail positions for one bone over the full scene range."""

    name: BlenderName
    head: tuple[tuple[FiniteFloat, FiniteFloat, FiniteFloat], ...]
    tail: tuple[tuple[FiniteFloat, FiniteFloat, FiniteFloat], ...]


class ChainFrames(KinesisModel):
    """Per-frame matrices for chain bones over the context window."""

    name: BlenderName
    world: tuple[Mat4, ...]
    pose_local: tuple[Quat, ...]


class ExtractResult(KinesisModel):
    protocol: Literal[1] = 1
    ok: Literal[True] = True
    armature_world: Mat4
    rest: tuple[BoneRest, ...]
    scene_frame_start: int
    scene_frame_end: int
    fps: float = Field(gt=0)
    context_frame_start: Frame
    chain: tuple[ChainFrames, ...]
    all_bones: tuple[BoneSamples, ...]
    original_action_hash: str


class ApplyRenderResult(KinesisModel):
    protocol: Literal[1] = 1
    ok: Literal[True] = True
    output_blend: RelPath
    original_action_hash_before: str
    original_action_hash_after: str
    all_bones: tuple[BoneSamples, ...]
    crop_frames: tuple[RelPath, ...] = ()
    context_frames: tuple[RelPath, ...] = ()
    render_ms: int = Field(default=0, ge=0)


class ExportResult(KinesisModel):
    protocol: Literal[1] = 1
    ok: Literal[True] = True
    output_blend: RelPath
    original_action_hash: str


class WorkerFailure(KinesisModel):
    protocol: Literal[1] = 1
    ok: Literal[False] = False
    error_type: Annotated[str, StringConstraints(max_length=100)]
    message: Annotated[str, StringConstraints(max_length=4000)]
