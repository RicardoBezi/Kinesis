"""Fault-injection scenarios (docs/FAILURE_MODES.md). They are activated in Phase 3, once the
DAG engine, the FakeJobRunner and the MockProvider exist.

Each id below matches a row of the failure-mode matrix. Each scenario asserts the retry
behaviour, the isolation scope, and that the error code a user sees is useful.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.fault, pytest.mark.skip(reason="Phase 3: orchestration engine")]

SCENARIOS = [
    "model_timeout_retried_then_fallback_plan",
    "model_429_respects_retry_after",
    "breaker_opens_after_threshold_and_fails_fast",
    "blender_subprocess_crash_retried_once",
    "redis_unavailable_degrades_to_memory_cache",
    "candidate_a_fails_candidate_b_succeeds_awaiting_decision",
    "both_candidates_fail_job_failed_no_viable_candidate",
    "render_timeout_isolated_to_candidate",
    "malformed_worker_result_not_retried",
    "serverless_job_failure_surfaces_error",
    "duplicate_status_event_deduplicated",
    "cancel_mid_run_cancels_child_tasks",
    "event_callback_exception_does_not_abort_dag",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_fault_scenario(scenario: str) -> None:
    raise NotImplementedError(scenario)
