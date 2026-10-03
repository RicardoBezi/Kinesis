"""Golden regression tests on the canonical fixture (docs/FIXTURE.md, TESTING.md).

The thresholds live in thresholds.json and the measured snapshots in foot_slide_v1.json
(written in Phase 2). Phase 0 checks only that the threshold contract is coherent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.golden

THRESHOLDS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "thresholds.json").read_text(encoding="utf-8")
)


def test_thresholds_agree_with_fixture_truth(fixture_truth: dict[str, Any]) -> None:
    assert THRESHOLDS["fixture"] == fixture_truth["fixture"]
    assert (
        THRESHOLDS["detection"]["planted_displacement_cm"]
        == fixture_truth["expected"]["planted_displacement_cm"]
    )
    assert THRESHOLDS["detection"]["severity"] == fixture_truth["expected"]["severity"]


def test_threshold_ordering_is_coherent() -> None:
    c = THRESHOLDS["candidates"]
    assert c["A"]["slip_reduction_pct_min"] >= c["best"]["slip_reduction_pct_min"] >= 80.0
    assert 0 < c["B"]["slip_reduction_pct_min"] <= c["A"]["slip_reduction_pct_min"]


@pytest.mark.skip(reason="Phase 1: detection on kinesis.testing.synthetic.foot_slide_v1()")
def test_detection_measures_injected_slide() -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 2: candidate A/B repair on the numpy mirror")
@pytest.mark.parametrize("label", ["A", "B"])
def test_candidate_meets_thresholds(label: str) -> None:
    raise NotImplementedError


@pytest.mark.skip(reason="Phase 2: preservation invariants (hypothesis) on the numpy mirror")
@pytest.mark.parametrize(
    "invariant",
    [
        "right_hand_unchanged_by_left_foot_repair",
        "input_arrays_not_mutated",
        "no_keys_outside_blend_window",
        "candidate_id_deterministic",
        "repair_idempotent",
    ],
)
def test_preservation_invariants(invariant: str) -> None:
    raise NotImplementedError
