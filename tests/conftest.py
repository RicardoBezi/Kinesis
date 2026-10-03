"""Shared test fixtures. Nothing here touches the network or Blender."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kinesis.schemas import AnimationSelection, SkeletalScope, TemporalScope

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_RESPONSES = Path(__file__).parent / "fixtures" / "ai_responses"
FIXTURE_TRUTH = REPO_ROOT / "blender" / "fixtures" / "fixture_truth.json"


@pytest.fixture(scope="session")
def fixture_truth() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE_TRUTH.read_text(encoding="utf-8"))
    return data


@pytest.fixture
def selection() -> AnimationSelection:
    """The canonical selection on foot_slide_v1 (see blender/fixtures/fixture_truth.json)."""
    return AnimationSelection(
        scene_id="scn_fixture01",
        armature="Rig",
        target_bones=("foot.L",),
        temporal=TemporalScope(frame_start=45, frame_end=90, context_before=10, context_after=10),
    )


@pytest.fixture
def scope() -> SkeletalScope:
    return SkeletalScope(
        armature="Rig",
        target_bones=("foot.L",),
        chain_bones=("thigh.L", "shin.L", "foot.L", "toe.L"),
        keyable_bones=("thigh.L", "shin.L", "foot.L"),
        context_bones=("pelvis", "root"),
    )


@pytest.fixture
def scene_range() -> tuple[int, int]:
    return (1, 120)


def load_ai_response(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((AI_RESPONSES / name).read_text(encoding="utf-8"))
    return data


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Live tests are double-gated: the marker must be selected AND the env flag must be 1."""
    if os.environ.get("KINESIS_LIVE_NEBIUS") == "1":
        return
    skip = pytest.mark.skip(reason="KINESIS_LIVE_NEBIUS != 1")
    for item in items:
        if "live_nebius" in item.keywords:
            item.add_marker(skip)
