"""Headless Blender integration tests. They run with ``uv run task test-blender``, or in the
CI ``blender.yml`` workflow.

They need a Blender 4.5 executable at KINESIS_BLENDER_BIN, plus the fixture built by
``uv run task fixture`` (Phase 1).
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.blender,
    pytest.mark.skipif(not os.environ.get("KINESIS_BLENDER_BIN"), reason="KINESIS_BLENDER_BIN"),
]

CHECKS = [
    ("scene_loads_and_inspect_reports_rig", "Phase 1"),
    ("extract_matches_numpy_mirror_within_0_1mm", "Phase 1"),
    ("autoexec_scripts_disabled", "Phase 1"),
    ("candidate_action_created_on_nla_track", "Phase 2"),
    ("original_action_fcurves_hash_unchanged", "Phase 2"),
    ("render_produces_expected_frame_count", "Phase 2"),
    ("metrics_produced_and_defect_reduced", "Phase 2"),
    ("export_writes_output_blend_with_selected_track", "Phase 2"),
]


@pytest.mark.parametrize(("check", "phase"), CHECKS)
def test_blender_worker(check: str, phase: str) -> None:
    pytest.skip(f"{phase}: {check}")
