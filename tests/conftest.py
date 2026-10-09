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
    gates = {"live_nebius": "KINESIS_LIVE_NEBIUS", "live_nebius_jobs": "KINESIS_LIVE_NEBIUS_JOBS"}
    for marker, env in gates.items():
        if os.environ.get(env) == "1":
            continue
        skip = pytest.mark.skip(reason=f"{env} != 1")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


# ---------------------------------------------------------------- Phase 3: service harness

FAKE_BLEND = b"BLENDER-v405" + b"\0" * 64  # passes the magic check; the fake runner ignores it


class SleepRecorder:
    """Injected in place of asyncio.sleep, so retries and backoff take no wall time."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def chunks(data: bytes, size: int = 1024) -> Any:
    for i in range(0, len(data), size):
        yield data[i : i + size]


@pytest.fixture
def make_service(tmp_path: Path) -> Any:
    """Factory: ``make_service(runner=..., provider=..., cache=..., **config)``."""
    from kinesis.jobs.service import JobService, ServiceConfig
    from kinesis.providers.null import NullProvider
    from kinesis.storage.sqlite import Database, SqliteArtifactStore, SqliteJobStore
    from kinesis.testing.fake_runner import FakeJobRunner

    created: list[Any] = []

    def factory(runner: Any = None, provider: Any = None, cache: Any = None, **config: Any) -> Any:
        db = Database(tmp_path / f"k{len(created)}.db")
        sleep = SleepRecorder()
        service = JobService(
            SqliteJobStore(db),
            SqliteArtifactStore(db, tmp_path / "jobs"),
            runner if runner is not None else FakeJobRunner(),
            provider if provider is not None else NullProvider(),
            ServiceConfig(data_dir=tmp_path, **config),
            cache=cache,
            sleep=sleep,
            jitter=lambda: 1.0,
        )
        service.sleep_recorder = sleep  # type: ignore[attr-defined]
        created.append(service)
        return service

    return factory


def job_request(**selection: Any) -> Any:
    from kinesis.schemas import CreateRepairJobRequest

    body = {
        "plan_mode": selection.pop("plan_mode", "AUTO"),
        "selection": {
            "scene_id": selection.pop("scene_id", "scn_placeholder"),
            "armature": "Rig",
            "target_bones": ["foot.L"],
            "temporal": {"frame_start": 45, "frame_end": 90},
            **selection,
        },
    }
    return CreateRepairJobRequest.model_validate(body)


async def run_job(service: Any, key: str = "key-00000001", **selection: Any) -> Any:
    """Upload the fake scene, create a job, wait for the DAG, return the stored job."""
    scene = await service.upload_scene(chunks(FAKE_BLEND))
    job, _ = await service.create_job(job_request(scene_id=scene.scene_id, **selection), key)
    await service.wait_idle()
    return await service.get_job(job.job_id)
