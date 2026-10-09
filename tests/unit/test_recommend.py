"""Recommendation rule: objective ranking, optionally blended with the visual review."""

from __future__ import annotations

import pytest

from kinesis.evaluation.recommend import recommend, visual_score
from kinesis.schemas import CandidateLabel, ObjectiveRank, VisualEvaluation

A, B = CandidateLabel.A, CandidateLabel.B


def rank(label: CandidateLabel, score: float | None, *, gated: bool = False) -> ObjectiveRank:
    return ObjectiveRank(
        label=label, candidate_id=label.value.lower() * 16, score=score, gated=gated
    )


def judged(label: CandidateLabel, s: int) -> VisualEvaluation:
    return VisualEvaluation(
        candidate_id=label.value.lower() * 16,
        contact_stability=s,
        naturalness=s,
        artifacts_visible=s,
        performance_preservation=s,
        instruction_adherence=s,
        model_id="m",
    )


def test_visual_score_maps_one_to_five_onto_unit_interval() -> None:
    assert visual_score(judged(A, 1)) == 0.0
    assert visual_score(judged(A, 5)) == 1.0
    assert visual_score(judged(A, 3)) == 0.5


def test_objective_only_without_complete_visual_review() -> None:
    ranking = [rank(B, 0.76), rank(A, 0.69)]
    rec = recommend(ranking, {}, visual_ok=False)
    assert rec.label is B
    assert "objective" in rec.reason
    assert "visual" not in rec.weights
    partial = recommend(ranking, {"b" * 16: judged(B, 5)}, visual_ok=True)
    assert "visual" not in partial.weights  # A has no judgement: objective decides


def test_visual_review_can_change_the_recommendation() -> None:
    ranking = [rank(B, 0.76), rank(A, 0.69)]
    visual = {"a" * 16: judged(A, 5), "b" * 16: judged(B, 2)}
    rec = recommend(ranking, visual, visual_ok=True)
    assert rec.label is A  # 0.7*0.69 + 0.3*1.0 = 0.783 > 0.7*0.76 + 0.3*0.25 = 0.607
    assert "measurements favour B" in rec.reason
    assert rec.weights["visual"] == pytest.approx(0.3)


def test_agreeing_views_keep_the_objective_winner() -> None:
    ranking = [rank(B, 0.76), rank(A, 0.69)]
    visual = {"a" * 16: judged(A, 4), "b" * 16: judged(B, 4)}
    rec = recommend(ranking, visual, visual_ok=True)
    assert rec.label is B
    assert "favour" not in rec.reason


def test_gated_candidates_are_never_recommended() -> None:
    ranking = [rank(B, 0.8), rank(A, None, gated=True)]
    visual = {"a" * 16: judged(A, 5), "b" * 16: judged(B, 1)}
    assert recommend(ranking, visual, visual_ok=True).label is B
    assert recommend([rank(A, None, gated=True)], {}, visual_ok=False).label is None
