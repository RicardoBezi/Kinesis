"""Write sample API payloads, built from the real Pydantic models, for the Kotlin tests.

    python scripts/export_samples.py          # write the samples (see OUT)
    python scripts/export_samples.py --check  # exit 1 if they are stale

The Kotlin serialization tests decode these files with the generated models. Python
validates the payloads and Kotlin parses the same bytes, which closes the contract loop.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from kinesis.repair.plan_validation import default_plan
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    AnimationSelection,
    ArtifactKind,
    ArtifactReference,
    CandidateLabel,
    CandidateMetrics,
    CandidateStatus,
    DecisionChoice,
    DefectReport,
    ErrorCode,
    EvaluationReport,
    EvaluatorStatus,
    EventPage,
    HumanDecision,
    JobEvent,
    JobEventType,
    JobStatus,
    ObjectiveRank,
    PlanMode,
    PlantedInterval,
    Problem,
    RepairCandidate,
    RepairJob,
    RepairType,
    Severity,
    SkeletalScope,
    TemporalScope,
    TokenUsage,
    VisualEvaluation,
    compute_candidate_id,
)
from kinesis.schemas.common import ALGORITHM_VERSION, KinesisModel

OUT = (
    Path(__file__).resolve().parent.parent
    / "client"
    / "kotlin-desktop"
    / "src"
    / "test"
    / "resources"
    / "samples"
)
T0 = datetime(2026, 10, 3, 12, 0, 0, tzinfo=UTC)
JOB = "job_01j9zsample"


def build() -> dict[str, KinesisModel]:
    selection = AnimationSelection(
        scene_id="scn_01j9zsample",
        armature="Rig",
        target_bones=("foot.L",),
        temporal=TemporalScope(frame_start=45, frame_end=90),
        instruction="keep the heel planted",
    )
    scope = SkeletalScope(
        armature="Rig",
        target_bones=("foot.L",),
        chain_bones=("thigh.L", "shin.L", "foot.L", "toe.L"),
        keyable_bones=("thigh.L", "shin.L", "foot.L"),
        context_bones=("pelvis", "root"),
    )
    interval = PlantedInterval(
        start=40,
        end=90,
        anchor=(0.1, -0.3, 0.0),
        planted_displacement_cm=10.0,
        path_slip_cm=10.0,
        max_frame_slip_cm=0.35,
        mean_slip_velocity_cm_s=4.7,
        velocity_variance=0.0004,
        contact_consistency=1.0,
    )
    defect = DefectReport(
        intervals=(interval,),
        worst_interval_index=0,
        severity=Severity.MAJOR,
        summary="foot.L planted 40-90 but slides 10.0 cm",
        algorithm_version=ALGORITHM_VERSION,
    )
    frames = ArtifactReference(
        artifact_id="art_01j9zframesa",
        kind=ArtifactKind.PREVIEW_FRAMES,
        media_type="image/jpeg",
        sha256="0" * 64,
        size_bytes=1_234_567,
        frame_count=67,
        first_frame=35,
        frame_step=1,
        uri="/v1/artifacts/art_01j9zframesa",
    )
    candidates = []
    for label, slip_after in ((CandidateLabel.A, 0.2), (CandidateLabel.B, 1.5)):
        params = DEFAULT_CANDIDATES[label]
        candidates.append(
            RepairCandidate(
                candidate_id=compute_candidate_id("sample-input", params),
                label=label,
                parameters=params,
                status=CandidateStatus.SUCCEEDED,
                artifacts=(frames,),
                metrics=CandidateMetrics(
                    planted_displacement_cm_before=10.0,
                    planted_displacement_cm_after=slip_after,
                    slip_reduction_pct=100 * (10.0 - slip_after) / 10.0,
                    contact_error_cm=0.1,
                    penetration_max_cm=0.0,
                    jerk_rms_ratio=1.2,
                    joint_limit_violations=0,
                    target_deviation_rms_cm=4.0,
                    collateral_max_cm=0.0,
                    outside_window_max_cm=0.0,
                    root_deviation_cm=0.0,
                    ik_unreachable_frames=0,
                    objective_score=0.9,
                ),
            )
        )
    evaluation = EvaluationReport(
        objective_ranking=tuple(
            ObjectiveRank(label=c.label, candidate_id=c.candidate_id, score=0.9, gated=False)
            for c in candidates
        ),
        visual=(
            VisualEvaluation(
                candidate_id=candidates[1].candidate_id,
                contact_stability=4,
                naturalness=5,
                artifacts_visible=5,
                performance_preservation=5,
                instruction_adherence=4,
                notes="Softer release; heel stays down.",
                model_id="nvidia/nemotron-placeholder",
                usage=TokenUsage(prompt_tokens=2048, completion_tokens=120),
                latency_ms=1800,
            ),
        ),
        recommended=CandidateLabel.B,
        recommendation_reason="B keeps more of the original weight shift.",
        evaluator_status=EvaluatorStatus.OK,
        scoring_weights={"slip": 0.55, "jerk": 0.2, "deviation": 0.15, "contact": 0.1},
    )
    decision = HumanDecision(
        job_id=JOB,
        choice=DecisionChoice.B,
        candidate_id=candidates[1].candidate_id,
        model_recommended=CandidateLabel.B,
        decided_at=T0,
        time_to_decision_s=41.5,
    )
    job = RepairJob(
        job_id=JOB,
        idempotency_key="sample-key-0001",
        repair_type=RepairType.FOOT_CONTACT,
        plan_mode=PlanMode.AUTO,
        selection=selection,
        status=JobStatus.AWAITING_DECISION,
        created_at=T0,
        updated_at=T0,
        skeletal_scope=scope,
        defect=defect,
        plan=default_plan(selection, scope, "no model provider configured"),
        candidates=tuple(candidates),
        evaluation=evaluation,
        decision=decision,
        last_event_seq=2,
    )
    events = EventPage(
        events=(
            JobEvent(
                seq=1,
                job_id=JOB,
                ts=T0,
                type=JobEventType.NODE_STARTED,
                node="extract_scope",
                attempt=1,
            ),
            JobEvent(
                seq=2,
                job_id=JOB,
                ts=T0,
                type=JobEventType.STATUS_CHANGED,
                message="RUNNING -> AWAITING_DECISION",
                data={"from": "RUNNING", "to": "AWAITING_DECISION", "elapsed_ms": 18234},
            ),
        ),
        next_seq=2,
    )
    problem = Problem(
        title="Bone not found",
        status=422,
        code=ErrorCode.BONE_NOT_FOUND,
        detail="foot.Left is not a bone of armature Rig",
        instance="/v1/jobs",
    )
    return {"repair_job": job, "event_page": events, "problem": problem}


def render() -> dict[str, str]:
    return {
        f"{name}.json": json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        for name, model in build().items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = []
    for name, text in render().items():
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            OUT.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode("utf-8"))
    if stale:
        print(f"stale samples {stale}: run `uv run task openapi`", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
