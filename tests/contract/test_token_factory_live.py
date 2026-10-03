"""Live Nebius Token Factory tests. They NEVER run by default.

Two gates must both be open: the ``live_nebius`` marker must be selected, and
``KINESIS_LIVE_NEBIUS=1`` must be set (see tests/conftest.py). Run them with
``uv run task live-nebius-test``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live_nebius


@pytest.mark.skip(reason="Phase 4: TokenFactoryProvider")
@pytest.mark.parametrize(
    "check",
    [
        "list_models_includes_configured_planner_and_vision_models",
        "planner_returns_valid_plan_for_fixture_defect",
        "vision_model_accepts_image_frames_and_returns_valid_judgement",
        "usage_tokens_reported",
    ],
)
def test_live(check: str) -> None:
    raise NotImplementedError(check)
