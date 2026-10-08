"""Behavioural API tests: the real app + JobService with a FakeJobRunner and MockProvider.

No Blender, no network. Requests go through ``httpx.ASGITransport`` in the test's event loop,
so background job tasks run on the same loop and ``service.wait_idle()`` replaces polling.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from conftest import FAKE_BLEND

from kinesis.main import create_app
from kinesis.providers.mock import MockProvider
from kinesis.testing.fake_runner import FakeJobRunner, Fault

PROBLEM = "application/problem+json"


class Api:
    def __init__(self, client: httpx.AsyncClient, service: Any) -> None:
        self.client = client
        self.service = service

    async def upload(self, data: bytes = FAKE_BLEND) -> httpx.Response:
        return await self.client.post(
            "/v1/scenes",
            files={"file": ("../../evil name.blend", data, "application/octet-stream")},
        )

    async def scene_id(self) -> str:
        r = await self.upload()
        assert r.status_code == 201, r.text
        scene_id: str = r.json()["scene_id"]
        return scene_id

    async def create(
        self, scene_id: str, key: str = "key-00000001", **overrides: Any
    ) -> httpx.Response:
        selection = {
            "scene_id": scene_id,
            "armature": "Rig",
            "target_bones": ["foot.L"],
            "temporal": {"frame_start": 45, "frame_end": 90},
            **overrides.pop("selection", {}),
        }
        body = {"selection": selection, **overrides}
        return await self.client.post("/v1/jobs", json=body, headers={"Idempotency-Key": key})

    async def ready_job(self, **overrides: Any) -> dict[str, Any]:
        r = await self.create(await self.scene_id(), **overrides)
        assert r.status_code == 202, r.text
        await self.service.wait_idle()
        job: dict[str, Any] = (await self.client.get(f"/v1/jobs/{r.json()['job_id']}")).json()
        return job


@pytest.fixture
async def api_factory(make_service: Any) -> AsyncIterator[Any]:
    clients: list[httpx.AsyncClient] = []

    async def factory(**service_kw: Any) -> Api:
        service = make_service(**service_kw)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)), base_url="http://test"
        )
        clients.append(client)
        return Api(client, service)

    yield factory
    for c in clients:
        await c.aclose()


@pytest.fixture
async def api(api_factory: Any) -> Api:
    result: Api = await api_factory(provider=MockProvider())
    return result


def assert_problem(r: httpx.Response, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    assert r.headers["content-type"].startswith(PROBLEM)
    assert r.json()["code"] == code


# ------------------------------------------------------------------ scenes


async def test_upload_inspects_scene_and_ignores_client_filename(api: Api) -> None:
    r = await api.upload()
    assert r.status_code == 201, r.text
    scene = r.json()
    assert scene["frame_start"] == 1
    assert scene["frame_end"] == 120
    assert scene["armatures"][0]["name"] == "Rig"
    stored = api.service.scene_dir(scene["scene_id"]) / "input" / "scene.blend"
    assert stored.read_bytes() == FAKE_BLEND
    assert (await api.client.get(f"/v1/scenes/{scene['scene_id']}")).json() == scene


async def test_upload_rejects_non_blend_and_oversized(api_factory: Any) -> None:
    api = await api_factory(max_upload_bytes=64)
    assert_problem(await api.upload(b"PK\x03\x04 a zip file"), 422, "FILE_INVALID")
    assert_problem(await api.upload(FAKE_BLEND), 413, "FILE_TOO_LARGE")
    assert_problem(await api.client.get("/v1/scenes/scn_missing01"), 404, "SCENE_NOT_FOUND")


# ------------------------------------------------------------------ happy path


async def test_post_job_202_then_poll_to_awaiting_decision(api: Api) -> None:
    r = await api.create(await api.scene_id())
    assert r.status_code == 202
    assert r.json()["status"] == "PENDING"
    await api.service.wait_idle()
    job = (await api.client.get(f"/v1/jobs/{r.json()['job_id']}")).json()
    assert job["status"] == "AWAITING_DECISION"
    defect = (await api.client.get(f"/v1/jobs/{job['job_id']}/defect")).json()
    assert defect["severity"] == "MAJOR"
    evaluation = (await api.client.get(f"/v1/jobs/{job['job_id']}/evaluation")).json()
    assert evaluation["recommended"] == "B"
    assert evaluation["evaluator_status"] == "OK"


async def test_get_candidates_and_metrics_after_completion(api: Api) -> None:
    job = await api.ready_job()
    candidates = (await api.client.get(f"/v1/jobs/{job['job_id']}/candidates")).json()
    assert [c["label"] for c in candidates] == ["A", "B"]
    for c in candidates:
        r = await api.client.get(f"/v1/jobs/{job['job_id']}/candidates/{c['candidate_id']}/metrics")
        assert r.status_code == 200
        assert r.json()["slip_reduction_pct"] >= 80
        frames = next(a for a in c["artifacts"] if a["kind"] == "PREVIEW_FRAMES")
        first = await api.client.get(f"{frames['uri']}?frame=0")
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/jpeg"
        assert first.content.startswith(b"\xff\xd8")
        last = frames["frame_count"] - 1
        assert (await api.client.get(f"{frames['uri']}?frame={last}")).status_code == 200
        assert_problem(
            await api.client.get(f"{frames['uri']}?frame={frames['frame_count']}"),
            404,
            "ARTIFACT_NOT_FOUND",
        )
    missing = await api.client.get(f"/v1/jobs/{job['job_id']}/candidates/0123456789abcdef/metrics")
    assert_problem(missing, 404, "CANDIDATE_NOT_FOUND")


async def test_post_decision_a_moves_to_applying_then_completed(api: Api) -> None:
    job = await api.ready_job()
    r = await api.client.post(
        f"/v1/jobs/{job['job_id']}/decision", json={"choice": "A", "time_to_decision_s": 12.5}
    )
    assert r.status_code == 202
    assert r.json()["status"] == "APPLYING"
    await api.service.wait_idle()
    done = (await api.client.get(f"/v1/jobs/{job['job_id']}")).json()
    assert done["status"] == "COMPLETED"
    assert done["decision"]["choice"] == "A"
    assert done["decision"]["model_recommended"] == "B"
    assert done["decision"]["agreed"] is False
    output = await api.client.get(done["output"]["uri"])
    assert output.status_code == 200
    assert output.content == b"BLENDER-fake-export"
    decisions = await api.service.store.list_decisions()
    assert [d.choice.value for d in decisions] == ["A"]


async def test_post_decision_reject_all_moves_to_rejected(api: Api) -> None:
    job = await api.ready_job()
    r = await api.client.post(f"/v1/jobs/{job['job_id']}/decision", json={"choice": "REJECT_ALL"})
    assert r.status_code == 202
    assert r.json()["status"] == "REJECTED"


async def test_decision_in_wrong_state_409_invalid_state(api: Api) -> None:
    job = await api.ready_job()
    await api.client.post(f"/v1/jobs/{job['job_id']}/decision", json={"choice": "REJECT_ALL"})
    again = await api.client.post(f"/v1/jobs/{job['job_id']}/decision", json={"choice": "A"})
    assert_problem(again, 409, "INVALID_STATE")
    assert_problem(await api.client.post(f"/v1/jobs/{job['job_id']}/cancel"), 409, "INVALID_STATE")


async def test_decision_for_failed_candidate_422(api_factory: Any) -> None:
    api = await api_factory(runner=FakeJobRunner({"apply_a": [Fault.CRASH] * 2}))
    job = await api.ready_job()
    assert job["status"] == "AWAITING_DECISION"
    r = await api.client.post(f"/v1/jobs/{job['job_id']}/decision", json={"choice": "A"})
    assert_problem(r, 422, "VALIDATION_ERROR")


async def test_cancel_awaiting_job(api: Api) -> None:
    job = await api.ready_job()
    r = await api.client.post(f"/v1/jobs/{job['job_id']}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"


# ------------------------------------------------------------------ validation


async def test_nonexistent_bone_422_bone_not_found(api: Api) -> None:
    r = await api.create(await api.scene_id(), selection={"target_bones": ["foot.X"]})
    assert_problem(r, 422, "BONE_NOT_FOUND")


async def test_missing_armature_422_armature_not_found(api: Api) -> None:
    r = await api.create(await api.scene_id(), selection={"armature": "Nope"})
    assert_problem(r, 422, "ARMATURE_NOT_FOUND")


async def test_end_before_start_422(api: Api) -> None:
    scene_id = await api.scene_id()
    r = await api.create(scene_id, selection={"temporal": {"frame_start": 90, "frame_end": 45}})
    assert_problem(r, 422, "VALIDATION_ERROR")
    r = await api.create(scene_id, selection={"temporal": {"frame_start": 100, "frame_end": 130}})
    assert_problem(r, 422, "INVALID_FRAME_RANGE")


async def test_unsupported_rig_and_unknown_scene(api: Api) -> None:
    scene_id = await api.scene_id()
    assert_problem(
        await api.create(scene_id, selection={"target_bones": ["pelvis"]}), 422, "UNSUPPORTED_RIG"
    )
    assert_problem(await api.create("scn_missing01"), 404, "SCENE_NOT_FOUND")


async def test_unsupported_repair_type_422(api: Api) -> None:
    r = await api.create(await api.scene_id(), repair_type="HAND_CONTACT")
    assert_problem(r, 422, "VALIDATION_ERROR")


async def test_malformed_model_plan_uses_fallback_and_emits_plan_rejected(api_factory: Any) -> None:
    api = await api_factory(provider=MockProvider(plans=['{"repair_type": "FOOT_CONTACT"']))
    job = await api.ready_job()
    assert job["plan"]["plan_source"] == "FALLBACK"
    page = (await api.client.get(f"/v1/jobs/{job['job_id']}/events?limit=500")).json()
    rejected = [e for e in page["events"] if e["type"] == "PLAN_REJECTED"]
    assert rejected
    assert rejected[0]["data"]["code"] == "PLAN_INVALID"


# ------------------------------------------------------------------ idempotency and events


async def test_idempotent_replay_returns_200_same_job(api: Api) -> None:
    scene_id = await api.scene_id()
    first = await api.create(scene_id)
    replay = await api.create(scene_id)
    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json()["job_id"] == first.json()["job_id"]
    await api.service.wait_idle()


async def test_idempotency_key_reuse_with_different_body_409(api: Api) -> None:
    scene_id = await api.scene_id()
    assert (await api.create(scene_id)).status_code == 202
    other = await api.create(scene_id, selection={"temporal": {"frame_start": 40, "frame_end": 90}})
    assert_problem(other, 409, "IDEMPOTENCY_CONFLICT")
    await api.service.wait_idle()


async def test_events_pagination_after_seq(api: Api) -> None:
    job = await api.ready_job()
    seen: list[int] = []
    after = 0
    while True:
        page = (
            await api.client.get(f"/v1/jobs/{job['job_id']}/events?after_seq={after}&limit=7")
        ).json()
        if not page["events"]:
            assert page["next_seq"] == after
            break
        seen += [e["seq"] for e in page["events"]]
        assert page["next_seq"] == page["events"][-1]["seq"]
        after = page["next_seq"]
    assert seen == list(range(1, job["last_event_seq"] + 1))
    types = {
        e["type"]
        for e in (await api.client.get(f"/v1/jobs/{job['job_id']}/events?limit=500")).json()[
            "events"
        ]
    }
    assert {"STATUS_CHANGED", "NODE_STARTED", "NODE_SUCCEEDED", "CANDIDATE_UPDATED"} <= types


async def test_artifact_path_traversal_impossible(api: Api) -> None:
    await api.ready_job()
    for bad in ("..%2F..%2Fkinesis.db", "art_..", "ART_UPPER", "%2e%2e"):
        r = await api.client.get(f"/v1/artifacts/{bad}")
        assert r.status_code in (404, 422), (bad, r.status_code)
    assert_problem(await api.client.get("/v1/artifacts/art_unknown001"), 404, "ARTIFACT_NOT_FOUND")
    assert_problem(await api.client.get("/v1/jobs/job_unknown001"), 404, "JOB_NOT_FOUND")


async def test_not_ready_resources_are_409(api_factory: Any) -> None:
    api = await api_factory(runner=FakeJobRunner({"extract_scope": [Fault.HANG]}))
    r = await api.create(await api.scene_id())
    job_id = r.json()["job_id"]
    assert_problem(await api.client.get(f"/v1/jobs/{job_id}/defect"), 409, "NOT_READY")
    assert_problem(await api.client.get(f"/v1/jobs/{job_id}/evaluation"), 409, "NOT_READY")
    cancelled = await api.client.post(f"/v1/jobs/{job_id}/cancel")
    assert cancelled.json()["status"] == "CANCELLED"


async def test_metrics_endpoint_counts_jobs_and_nodes(api: Api) -> None:
    await api.ready_job()
    text = (await api.client.get("/v1/metrics")).text
    assert 'kinesis_jobs_finished_total{status="AWAITING_DECISION"}' in text
    assert 'kinesis_node_seconds_count{node="extract_scope",status="SUCCEEDED"}' in text
