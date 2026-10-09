"""API contract tests for Phase 0: routes exist, errors are Problems, OpenAPI is current.

Behavioural API tests (happy path, invalid bone or range, idempotency, decision state) are in
tests/integration/test_api_jobs.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kinesis.main import create_app

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import export_openapi  # noqa: E402

REQUIRED_SCHEMAS = {
    # spec §7 minimum contract set
    "RepairJob",
    "AnimationSelection",
    "TemporalScope",
    "SkeletalScope",
    "ContactTarget",
    "DefectReport",
    "RepairPlan",
    "RepairCandidate",
    "CandidateMetrics",
    "VisualEvaluation",
    "EvaluationReport",
    "HumanDecision",
    "ArtifactReference",
    "JobStatus",
    "JobEvent",
    "Problem",
}


@pytest.fixture
def client(make_service: Any) -> TestClient:
    return TestClient(create_app(make_service()))


def test_health(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_metrics_endpoint_is_prometheus_text(client: TestClient) -> None:
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")


VALID_JOB = {
    "selection": {
        "scene_id": "scn_fixture01",
        "armature": "Rig",
        "target_bones": ["foot.L"],
        "temporal": {"frame_start": 45, "frame_end": 90},
    }
}


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "code"),
    [
        ("get", "/v1/jobs/job_000001", {}, "JOB_NOT_FOUND"),
        ("get", "/v1/jobs/job_000001/events?after_seq=3", {}, "JOB_NOT_FOUND"),
        ("get", "/v1/jobs/job_000001/defect", {}, "JOB_NOT_FOUND"),
        ("get", "/v1/jobs/job_000001/candidates", {}, "JOB_NOT_FOUND"),
        ("get", "/v1/jobs/job_000001/candidates/0123456789abcdef/metrics", {}, "JOB_NOT_FOUND"),
        ("get", "/v1/jobs/job_000001/evaluation", {}, "JOB_NOT_FOUND"),
        ("post", "/v1/jobs/job_000001/decision", {"json": {"choice": "A"}}, "JOB_NOT_FOUND"),
        ("post", "/v1/jobs/job_000001/cancel", {}, "JOB_NOT_FOUND"),
        (
            "post",
            "/v1/jobs",
            {"json": VALID_JOB, "headers": {"Idempotency-Key": "key-0001"}},
            "SCENE_NOT_FOUND",
        ),
        ("get", "/v1/scenes/scn_fixture01", {}, "SCENE_NOT_FOUND"),
        ("get", "/v1/artifacts/art_000001?frame=3", {}, "ARTIFACT_NOT_FOUND"),
    ],
)
def test_unknown_resources_are_404_problems(
    client: TestClient, method: str, path: str, kwargs: dict[str, object], code: str
) -> None:
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 404, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == code


def test_provider_health_reports_every_component(client: TestClient) -> None:
    r = client.get("/v1/health/providers")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert [c["name"] for c in body["components"]] == ["model_provider", "redis", "blender"]


@pytest.mark.parametrize("path", ["/v1/stats/product"])
def test_remaining_stubs_return_problem_501(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 501, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "NOT_IMPLEMENTED"


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        ({**VALID_JOB}, {}),  # missing Idempotency-Key
        (VALID_JOB, {"Idempotency-Key": "short"}),
        (
            {
                "selection": {
                    **VALID_JOB["selection"],
                    "temporal": {"frame_start": 90, "frame_end": 45},
                }
            },
            {"Idempotency-Key": "key-0001"},
        ),
        ({**VALID_JOB, "repair_type": "HAND_CONTACT"}, {"Idempotency-Key": "key-0001"}),
        ({**VALID_JOB, "unexpected": 1}, {"Idempotency-Key": "key-0001"}),
    ],
)
def test_invalid_job_requests_are_422_problems(
    client: TestClient, body: dict[str, object], headers: dict[str, str]
) -> None:
    r = client.post("/v1/jobs", json=body, headers=headers)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "VALIDATION_ERROR"


def test_path_ids_are_validated(client: TestClient) -> None:
    assert client.get("/v1/jobs/..%2Fetc").status_code in (404, 422)
    assert client.get("/v1/artifacts/UPPER").status_code == 422


def test_openapi_document_is_committed_and_current() -> None:
    committed = export_openapi.OUT.read_text(encoding="utf-8")
    assert committed == export_openapi.render(), "run `uv run task openapi`"


def test_openapi_contains_frozen_contract() -> None:
    spec = json.loads(export_openapi.render())
    schemas = set(spec["components"]["schemas"])
    missing = REQUIRED_SCHEMAS - schemas
    assert not missing, missing
    assert not [s for s in schemas if s.endswith(("-Input", "-Output"))]
    assert "HTTPValidationError" not in schemas


def test_kotlin_samples_are_current() -> None:
    import export_samples

    for name, text in export_samples.render().items():
        path = export_samples.OUT / name
        assert path.read_text(encoding="utf-8") == text, f"{name} stale: run `uv run task openapi`"
