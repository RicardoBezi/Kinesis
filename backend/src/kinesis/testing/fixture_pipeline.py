"""The whole deterministic pipeline on the numpy mirror: detect -> repair -> apply -> measure.

Golden tests use this; Phase 3's FakeJobRunner can reuse the same pieces. Nothing here needs
Blender.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from kinesis.analysis.detection import Detection, detect_foot_slide
from kinesis.analysis.extraction import ExtractedMotion, foot_and_toe, motion_from_extract
from kinesis.evaluation.metrics import MetricContext, Samples, compute_metrics
from kinesis.repair.candidates import CandidateResult, LegChain, generate_candidate, repair_input
from kinesis.schemas.candidate import CandidateMetrics
from kinesis.schemas.common import CandidateLabel
from kinesis.schemas.plan import DEFAULT_CANDIDATES, CandidateParameters
from kinesis.testing.fake_worker import apply_keys, extract_result
from kinesis.testing.synthetic import SyntheticScene, foot_slide_v1

LEFT_LEG = LegChain("thigh.L", "shin.L", "foot.L", "toe.L")
CHAIN_BONES = ("thigh.L", "shin.L", "foot.L", "toe.L")
ROOT_BONES = ("root", "pelvis")
SELECTION = (45, 90)
CONTEXT = (35, 100)


def scene_samples(scene: SyntheticScene) -> Samples:
    names = scene.skeleton.names
    return Samples(
        frame_start=scene.frame_start,
        heads={n: scene.head(n) for n in names},
        tails={n: scene.tail(n) for n in names},
    )


@dataclass(frozen=True)
class CandidateRun:
    parameters: CandidateParameters
    result: CandidateResult
    scene: SyntheticScene
    metrics: CandidateMetrics


@dataclass(frozen=True)
class FixtureRun:
    scene: SyntheticScene
    motion: ExtractedMotion
    detection: Detection
    interval: tuple[int, int]
    candidates: dict[CandidateLabel, CandidateRun] = field(default_factory=dict)


def metric_context(
    params: CandidateParameters, interval: tuple[int, int], floor_height: float
) -> MetricContext:
    return MetricContext(
        fps=24.0,
        thigh=LEFT_LEG.thigh,
        shin=LEFT_LEG.shin,
        foot=LEFT_LEG.foot,
        toe=LEFT_LEG.toe,
        chain_bones=frozenset(CHAIN_BONES),
        root_bones=ROOT_BONES,
        interval=interval,
        blend_frames=params.blend_frames,
        context_range=CONTEXT,
        floor_height=floor_height,
    )


def run_fixture(
    candidates: Mapping[CandidateLabel, CandidateParameters] = DEFAULT_CANDIDATES,
    *,
    scene: SyntheticScene | None = None,
) -> FixtureRun:
    scene = scene or foot_slide_v1()
    motion = motion_from_extract(extract_result(scene, CHAIN_BONES, CONTEXT))
    ankle, ball = foot_and_toe(motion, LEFT_LEG.foot, LEFT_LEG.toe)
    detection = detect_foot_slide(
        ankle, ball, frame_start=CONTEXT[0], fps=motion.fps, selection=SELECTION, bone="foot.L"
    )
    worst = detection.report.worst
    if worst is None:
        raise ValueError("no planted interval in the selection")
    interval = (worst.start, worst.end)
    inp = repair_input(motion, LEFT_LEG, interval, detection.floor_height)
    original = scene_samples(scene)
    runs: dict[CandidateLabel, CandidateRun] = {}
    for label, params in candidates.items():
        result = generate_candidate(inp, params)
        repaired = apply_keys(scene, result.bone_keys())
        metrics = compute_metrics(
            original,
            scene_samples(repaired),
            metric_context(params, interval, detection.floor_height),
            result.ik_unreachable_frames,
        )
        runs[label] = CandidateRun(params, result, repaired, metrics)
    return FixtureRun(scene, motion, detection, interval, runs)


__all__ = [
    "CHAIN_BONES",
    "CONTEXT",
    "LEFT_LEG",
    "SELECTION",
    "CandidateRun",
    "FixtureRun",
    "metric_context",
    "run_fixture",
    "scene_samples",
]
