"""Product metrics (METRICS.md §1) from stored jobs, decisions and costs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kinesis.jobs.stats import compute_product_stats
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    CandidateLabel,
    CandidateMetrics,
    CandidateStatus,
    DecisionChoice,
    HumanDecision,
    JobStatus,
    PlanMode,
    RepairCandidate,
    RepairJob,
    RepairType,
)
from kinesis.schemas.scope import AnimationSelection

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def metrics(slip: float) -> CandidateMetrics:
    return CandidateMetrics(
        planted_displacement_cm_before=10,
        planted_displacement_cm_after=10 - slip / 10,
        slip_reduction_pct=slip,
        contact_error_cm=0,
        penetration_max_cm=0,
        jerk_rms_ratio=1,
        joint_limit_violations=0,
        target_deviation_rms_cm=1,
        collateral_max_cm=0,
        outside_window_max_cm=0,
        root_deviation_cm=0,
        ik_unreachable_frames=0,
    )


def job(job_id: str, selection: AnimationSelection, slips: tuple[float, float]) -> RepairJob:
    candidates = tuple(
        RepairCandidate(
            candidate_id=f"{job_id[-4:]}{label.value.lower()}".rjust(16, "0"),
            label=label,
            parameters=DEFAULT_CANDIDATES[label],
            status=CandidateStatus.SUCCEEDED,
            metrics=metrics(slip),
        )
        for label, slip in zip(CandidateLabel, slips, strict=True)
    )
    return RepairJob(
        job_id=job_id,
        idempotency_key="key-00000001",
        repair_type=RepairType.FOOT_CONTACT,
        plan_mode=PlanMode.AUTO,
        selection=selection,
        status=JobStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW,
        candidates=candidates,
    )


def decision(
    j: RepairJob, choice: DecisionChoice, rec: CandidateLabel | None, t: float
) -> HumanDecision:
    cid = next((c.candidate_id for c in j.candidates if c.label.value == choice.value), None)
    return HumanDecision(
        job_id=j.job_id,
        choice=choice,
        candidate_id=cid,
        model_recommended=rec,
        decided_at=NOW,
        time_to_decision_s=t,
    )


def test_empty_store() -> None:
    stats = compute_product_stats([], [], {})
    assert stats.jobs_total == 0
    assert stats.candidate_acceptance_rate is None
    assert stats.cost_per_accepted_repair_usd is None


def test_rates_medians_and_cost(selection: AnimationSelection) -> None:
    j1, j2, j3 = (job(f"job_00000{i}", selection, (100.0, 88.0)) for i in (1, 2, 3))
    decisions = [
        decision(j1, DecisionChoice.B, CandidateLabel.B, 30),  # agreed, slip 88
        decision(j2, DecisionChoice.A, CandidateLabel.B, 50),  # disagreed, slip 100
        decision(j3, DecisionChoice.REJECT_ALL, None, 10),
    ]
    costs = {"job_000001": (0.002, 0), "job_000002": (0.004, 0)}
    stats = compute_product_stats([j1, j2, j3], decisions, costs)
    assert stats.jobs_total == 3
    assert stats.jobs_decided == 3
    assert stats.candidate_acceptance_rate == pytest.approx(2 / 3)
    assert stats.model_human_agreement_rate == pytest.approx(0.5)
    assert stats.median_slip_reduction_pct == pytest.approx(94.0)
    assert stats.median_time_to_decision_s == pytest.approx(30.0)
    assert stats.cost_per_accepted_repair_usd == pytest.approx(0.003)


def test_cost_is_unknown_when_any_call_was_unpriced(selection: AnimationSelection) -> None:
    j = job("job_000001", selection, (100.0, 88.0))
    stats = compute_product_stats(
        [j], [decision(j, DecisionChoice.A, None, 1)], {"job_000001": (0.0, 2)}
    )
    assert stats.cost_per_accepted_repair_usd is None
