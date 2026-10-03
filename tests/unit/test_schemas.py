"""Contract tests for the frozen schemas."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    JOB_TRANSITIONS,
    AnchorMode,
    AnimationSelection,
    CandidateLabel,
    CandidateParameters,
    ContactKind,
    ContactTarget,
    DecisionChoice,
    DefectReport,
    HumanDecision,
    JobStatus,
    PlanSource,
    RepairPlan,
    RepairType,
    Severity,
    SkeletalScope,
    TemporalScope,
    compute_candidate_id,
)
from kinesis.schemas.common import ALGORITHM_VERSION

# ------------------------------------------------------------------ temporal scope


def test_temporal_scope_context_window() -> None:
    t = TemporalScope(frame_start=83, frame_end=112)
    assert (t.context_start, t.context_end) == (73, 122)
    assert t.clipped(1, 115) == (73, 115)


def test_temporal_scope_rejects_inverted_range() -> None:
    with pytest.raises(ValidationError, match="frame_end must be >= frame_start"):
        TemporalScope(frame_start=50, frame_end=40)


def test_temporal_scope_rejects_oversized_window() -> None:
    with pytest.raises(ValidationError, match="exceeds 600"):
        TemporalScope(frame_start=0, frame_end=700)


def test_models_are_frozen_and_forbid_extra() -> None:
    t = TemporalScope(frame_start=1, frame_end=2)
    with pytest.raises(ValidationError):
        t.frame_start = 5  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs"):
        TemporalScope.model_validate({"frame_start": 1, "frame_end": 2, "evil": True})


# ------------------------------------------------------------------ skeletal scope / names


def test_skeletal_scope_requires_targets_in_chain() -> None:
    with pytest.raises(ValidationError, match="subset of chain_bones"):
        SkeletalScope(
            armature="Rig",
            target_bones=("hand.R",),
            chain_bones=("thigh.L",),
            keyable_bones=("thigh.L",),
        )


@pytest.mark.parametrize("name", ["", "a" * 64, "foot;rm -rf", "../etc", "foot\nL"])
def test_bone_names_are_constrained(name: str) -> None:
    with pytest.raises(ValidationError):
        AnimationSelection(
            scene_id="scn_fixture01",
            armature="Rig",
            target_bones=(name,),
            temporal=TemporalScope(frame_start=1, frame_end=2),
        )


def test_selection_rejects_duplicate_bones() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AnimationSelection(
            scene_id="scn_fixture01",
            armature="Rig",
            target_bones=("foot.L", "foot.L"),
            temporal=TemporalScope(frame_start=1, frame_end=2),
        )


def test_contact_target_validation() -> None:
    assert ContactTarget().plane_normal == (0.0, 0.0, 1.0)
    with pytest.raises(ValidationError, match="unit vector"):
        ContactTarget(plane_normal=(0.0, 0.0, 2.0))
    with pytest.raises(ValidationError, match="object_name"):
        ContactTarget(kind=ContactKind.OBJECT)
    with pytest.raises(ValidationError):
        ContactTarget(plane_point=(float("nan"), 0.0, 0.0))


# ------------------------------------------------------------------ candidate parameters


@pytest.mark.parametrize(
    "override",
    [
        {"lock_strength": 1.5},
        {"lock_strength": -0.1},
        {"blend_frames": 25},
        {"tolerance_cm": 6.0},
        {"smoothing_window": 4},
        {"anchor_mode": "TELEPORT"},
    ],
)
def test_candidate_parameters_bounds(override: dict[str, object]) -> None:
    base = DEFAULT_CANDIDATES[CandidateLabel.A].model_dump()
    with pytest.raises(ValidationError):
        CandidateParameters.model_validate({**base, **override})


def test_default_presets_match_algorithms_doc() -> None:
    a, b = DEFAULT_CANDIDATES[CandidateLabel.A], DEFAULT_CANDIDATES[CandidateLabel.B]
    assert (a.lock_strength, a.anchor_mode, a.blend_frames) == (1.0, AnchorMode.ONSET, 3)
    assert (b.lock_strength, b.anchor_mode, b.blend_frames) == (0.85, AnchorMode.MEAN, 8)
    assert a.lock_yaw
    assert not b.lock_yaw
    assert (a.smoothing_window, b.smoothing_window) == (0, 5)


# ------------------------------------------------------------------ candidate identity

params_strategy = st.builds(
    CandidateParameters,
    lock_strength=st.floats(0, 1),
    anchor_mode=st.sampled_from(AnchorMode),
    blend_frames=st.integers(0, 24),
    tolerance_cm=st.floats(0, 5),
    lock_yaw=st.booleans(),
    smoothing_window=st.sampled_from([0, 3, 5, 7, 9]),
    height_clamp=st.booleans(),
)


@given(params=params_strategy, input_hash=st.text(min_size=1, max_size=64))
def test_candidate_id_is_deterministic(params: CandidateParameters, input_hash: str) -> None:
    rebuilt = CandidateParameters.model_validate(params.model_dump())
    first = compute_candidate_id(input_hash, params)
    assert first == compute_candidate_id(input_hash, rebuilt)
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


def test_candidate_id_changes_with_inputs() -> None:
    a = DEFAULT_CANDIDATES[CandidateLabel.A]
    b = DEFAULT_CANDIDATES[CandidateLabel.B]
    base = compute_candidate_id("h1", a)
    assert base != compute_candidate_id("h2", a)
    assert base != compute_candidate_id("h1", b)
    assert base != compute_candidate_id("h1", a, algorithm_version="foot_contact/2")
    assert ALGORITHM_VERSION == "foot_contact/1"


# ------------------------------------------------------------------ plan


def _plan(**overrides: object) -> RepairPlan:
    data: dict[str, object] = {
        "repair_type": RepairType.FOOT_CONTACT,
        "target_bones": ("foot.L",),
        "frame_start": 45,
        "frame_end": 90,
        "context_frames_before": 10,
        "context_frames_after": 10,
        "contact_target": ContactTarget(),
        "candidates": dict(DEFAULT_CANDIDATES),
        "explanation": "x",
        "plan_source": PlanSource.FALLBACK,
        "fallback_reason": "test",
    }
    data.update(overrides)
    return RepairPlan.model_validate(data)


def test_plan_requires_both_candidates() -> None:
    with pytest.raises(ValidationError, match="exactly candidates A and B"):
        _plan(candidates={CandidateLabel.A: DEFAULT_CANDIDATES[CandidateLabel.A]})


def test_plan_preservation_flags_cannot_be_disabled() -> None:
    with pytest.raises(ValidationError):
        _plan(preserve_non_target_bones=False)
    with pytest.raises(ValidationError):
        _plan(preserve_root_motion=False)


def test_plan_provenance_rules() -> None:
    with pytest.raises(ValidationError, match="model_id"):
        _plan(plan_source=PlanSource.MODEL, fallback_reason=None)
    with pytest.raises(ValidationError, match="fallback_reason"):
        _plan(fallback_reason=None)


def test_plan_rejects_unknown_repair_type() -> None:
    with pytest.raises(ValidationError):
        _plan(repair_type="FULL_BODY_REGEN")


# ------------------------------------------------------------------ defect report


def test_empty_defect_report_must_be_none_severity() -> None:
    ok = DefectReport(intervals=(), severity=Severity.NONE, summary="", algorithm_version="v")
    assert ok.worst is None
    with pytest.raises(ValidationError):
        DefectReport(intervals=(), severity=Severity.MAJOR, summary="", algorithm_version="v")


# ------------------------------------------------------------------ jobs / decisions


def test_job_transitions_terminal_states_are_final() -> None:
    for status in JobStatus:
        if status.is_terminal:
            assert JOB_TRANSITIONS[status] == frozenset()
        else:
            assert JOB_TRANSITIONS[status], status
    assert JobStatus.APPLYING in JOB_TRANSITIONS[JobStatus.AWAITING_DECISION]
    assert JobStatus.APPLYING not in JOB_TRANSITIONS[JobStatus.RUNNING]


@pytest.mark.parametrize(
    ("choice", "recommended", "agreed"),
    [
        (DecisionChoice.A, CandidateLabel.A, True),
        (DecisionChoice.B, CandidateLabel.A, False),
        (DecisionChoice.REJECT_ALL, CandidateLabel.B, False),
        (DecisionChoice.A, None, None),
    ],
)
def test_human_decision_agreement(
    choice: DecisionChoice, recommended: CandidateLabel | None, agreed: bool | None
) -> None:
    d = HumanDecision(
        job_id="job_000001",
        choice=choice,
        model_recommended=recommended,
        decided_at=datetime(2026, 10, 3, tzinfo=UTC),
    )
    assert d.agreed is agreed
    assert d.model_dump(mode="json")["agreed"] is agreed
