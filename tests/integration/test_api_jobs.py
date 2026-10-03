"""Behavioural API tests: TestClient + FakeJobRunner + MockProvider, without Blender or the
network. They are activated in Phase 3.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(reason="Phase 3: job handlers + FakeJobRunner")

CASES = [
    "post_job_202_then_poll_to_awaiting_decision",
    "get_candidates_and_metrics_after_completion",
    "post_decision_a_moves_to_applying_then_completed",
    "post_decision_reject_all_moves_to_rejected",
    "decision_in_wrong_state_409_invalid_state",
    "decision_for_failed_candidate_422",
    "nonexistent_bone_422_bone_not_found",
    "missing_armature_422_armature_not_found",
    "end_before_start_422",
    "unsupported_repair_type_422",
    "malformed_model_plan_uses_fallback_and_emits_plan_rejected",
    "idempotent_replay_returns_200_same_job",
    "idempotency_key_reuse_with_different_body_409",
    "events_pagination_after_seq",
    "artifact_path_traversal_impossible",
]


@pytest.mark.parametrize("case", CASES)
def test_api_behaviour(case: str) -> None:
    raise NotImplementedError(case)
