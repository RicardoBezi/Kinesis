"""Repair job endpoints (docs/API.md). Phase 0 freezes the contracts; handlers return 501."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Path, Query, status

from kinesis.api.errors import not_implemented, problem_responses
from kinesis.schemas import (
    CandidateMetrics,
    CreateRepairJobRequest,
    DecisionRequest,
    DefectReport,
    EvaluationReport,
    EventPage,
    RepairCandidate,
    RepairJob,
)
from kinesis.schemas.job import IdempotencyKey

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

JobIdPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{5,63}$")]
CandidateIdPath = Annotated[str, Path(pattern=r"^[0-9a-f]{16}$")]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RepairJob,
    responses={
        200: {"model": RepairJob, "description": "Idempotent replay: the existing job"},
        **problem_responses(404, 409, 422, 501),
    },
    summary="Create a repair job",
    description=(
        "Validates the selection against the uploaded scene's inventory (armature, bones, "
        "frame range) and schedules the repair DAG. A replay with the same `Idempotency-Key` "
        "and body returns 200 with the existing job. The same key with a different body "
        "returns 409 IDEMPOTENCY_CONFLICT."
    ),
)
async def create_job(
    body: CreateRepairJobRequest,
    idempotency_key: Annotated[IdempotencyKey, Header(alias="Idempotency-Key")],
) -> RepairJob:
    raise not_implemented("Phase 3")


@router.get(
    "/{job_id}",
    response_model=RepairJob,
    responses=problem_responses(404, 501),
    summary="Get a job",
)
async def get_job(job_id: JobIdPath) -> RepairJob:
    raise not_implemented("Phase 3")


@router.get(
    "/{job_id}/events",
    response_model=EventPage,
    responses=problem_responses(404, 501),
    summary="Poll job events after a sequence number",
)
async def list_events(
    job_id: JobIdPath,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventPage:
    raise not_implemented("Phase 3")


@router.get(
    "/{job_id}/defect",
    response_model=DefectReport,
    responses=problem_responses(404, 409, 501),
    summary="Deterministic defect report (409 NOT_READY until analysis completes)",
)
async def get_defect(job_id: JobIdPath) -> DefectReport:
    raise not_implemented("Phase 3")


@router.get(
    "/{job_id}/candidates",
    response_model=list[RepairCandidate],
    responses=problem_responses(404, 501),
    summary="List repair candidates",
)
async def list_candidates(job_id: JobIdPath) -> list[RepairCandidate]:
    raise not_implemented("Phase 2")


@router.get(
    "/{job_id}/candidates/{candidate_id}/metrics",
    response_model=CandidateMetrics,
    responses=problem_responses(404, 409, 501),
    summary="Objective metrics for one candidate",
)
async def get_candidate_metrics(
    job_id: JobIdPath, candidate_id: CandidateIdPath
) -> CandidateMetrics:
    raise not_implemented("Phase 2")


@router.get(
    "/{job_id}/evaluation",
    response_model=EvaluationReport,
    responses=problem_responses(404, 409, 501),
    summary="Combined objective + model evaluation",
)
async def get_evaluation(job_id: JobIdPath) -> EvaluationReport:
    raise not_implemented("Phase 4")


@router.post(
    "/{job_id}/decision",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RepairJob,
    responses=problem_responses(404, 409, 422, 501),
    summary="Record the animator's choice",
    description=(
        "Allowed only in AWAITING_DECISION (otherwise 409 INVALID_STATE). Choosing A or B "
        "schedules the non-destructive apply (status APPLYING). REJECT_ALL moves the job to "
        "REJECTED. Choosing a FAILED candidate returns 422."
    ),
)
async def post_decision(job_id: JobIdPath, body: DecisionRequest) -> RepairJob:
    raise not_implemented("Phase 5")


@router.post(
    "/{job_id}/cancel",
    response_model=RepairJob,
    responses=problem_responses(404, 409, 501),
    summary="Cancel a non-terminal job",
)
async def cancel_job(job_id: JobIdPath) -> RepairJob:
    raise not_implemented("Phase 3")
