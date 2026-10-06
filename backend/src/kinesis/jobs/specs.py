"""Builders for Phase 2 worker specs, with the job-directory layout of io_contract.md."""

from __future__ import annotations

from kinesis.jobs.worker_io import spec_paths
from kinesis.schemas.common import CandidateLabel
from kinesis.schemas.worker import ApplyRenderSpec, BoneKeys, ExportSpec, RenderSpec

ORIGINAL_TRACK = "Kinesis:Original"


def track_name(label: CandidateLabel) -> str:
    return f"Kinesis:{label.value}"


def action_name(job_id: str, label: CandidateLabel) -> str:
    return f"KIN_{job_id}_{label.value}"[:63]


def candidate_apply_spec(
    job_id: str,
    label: CandidateLabel,
    candidate_id: str,
    armature: str,
    keys: tuple[BoneKeys, ...],
    render: RenderSpec | None,
) -> tuple[str, ApplyRenderSpec]:
    """``(node, spec)`` for one candidate's ``apply_render``."""
    node = f"apply_{label.value.lower()}"
    base = f"candidates/{candidate_id}"
    return node, ApplyRenderSpec(
        armature=armature,
        action_name=action_name(job_id, label),
        nla_track_name=track_name(label),
        keys=keys,
        render=render,
        output_blend=f"{base}/candidate.blend",
        frames_dir=f"{base}/frames",
        result_path=spec_paths(node)[1],
    )


def original_render_spec(armature: str, render: RenderSpec) -> tuple[str, ApplyRenderSpec]:
    """``(node, spec)`` rendering the untouched original with the same cameras."""
    node = "render_original"
    return node, ApplyRenderSpec(
        armature=armature,
        action_name="KIN_original",
        nla_track_name=ORIGINAL_TRACK,
        keys=(),
        render=render,
        render_original=True,
        output_blend="original/original.blend",
        frames_dir="original/frames",
        result_path=spec_paths(node)[1],
    )


def export_spec(
    scene_stem: str, armature: str, label: CandidateLabel, candidate_id: str
) -> tuple[str, ExportSpec]:
    node = "export"
    return node, ExportSpec(
        candidate_blend=f"candidates/{candidate_id}/candidate.blend",
        armature=armature,
        keep_track=track_name(label),
        output_blend=f"output/{scene_stem}_kinesis.blend",
        result_path=spec_paths(node)[1],
    )


__all__ = [
    "ORIGINAL_TRACK",
    "action_name",
    "candidate_apply_spec",
    "export_spec",
    "original_render_spec",
    "track_name",
]
