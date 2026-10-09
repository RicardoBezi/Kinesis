"""Repair job endpoints (docs/API.md)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Query, Response, status

from kinesis.api.deps import get_service
from kinesis.api.errors import problem_responses
from kinesis.jobs.service import JobService
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
Service = Annotated[JobService, Depends(get_service)]


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RepairJob,
    responses={
        200: {"model": RepairJob, "description": "Idempotent replay: the existing job"},
        **problem_responses(404, 409, 422),
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
    response: Response,
    service: Service,
) -> RepairJob:
    job, created = await service.create_job(body, idempotency_key)
    if not created:
        response.status_code = status.HTTP_200_OK
    return job


@router.get(
    "",
    response_model=list[RepairJob],
    responses=problem_responses(),
    summary="List jobs, newest first",
    description="Pages backwards with `before=<job_id>` (ids sort by creation time).",
)
async def list_jobs(
    service: Service,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9_-]{5,63}$")] = None,
) -> list[RepairJob]:
    return await service.list_jobs(limit, before)


@router.get(
    "/{job_id}",
    response_model=RepairJob,
    responses=problem_responses(404),
    summary="Get a job",
)
async def get_job(job_id: JobIdPath, service: Service) -> RepairJob:
    return await service.get_job(job_id)


@router.get(
    "/{job_id}/events",
    response_model=EventPage,
    responses=problem_responses(404),
    summary="Poll job events after a sequence number",
)
async def list_events(
    job_id: JobIdPath,
    service: Service,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventPage:
    return await service.events(job_id, after_seq, limit)


@router.get(
    "/{job_id}/defect",
    response_model=DefectReport,
    responses=problem_responses(404, 409),
    summary="Deterministic defect report (409 NOT_READY until analysis completes)",
)
async def get_defect(job_id: JobIdPath, service: Service) -> DefectReport:
    return await service.defect(job_id)


@router.get(
    "/{job_id}/candidates",
    response_model=list[RepairCandidate],
    responses=problem_responses(404),
    summary="List repair candidates",
)
async def list_candidates(job_id: JobIdPath, service: Service) -> list[RepairCandidate]:
    return await service.candidates(job_id)


@router.get(
    "/{job_id}/candidates/{candidate_id}/metrics",
    response_model=CandidateMetrics,
    responses=problem_responses(404, 409),
    summary="Objective metrics for one candidate",
)
async def get_candidate_metrics(
    job_id: JobIdPath, candidate_id: CandidateIdPath, service: Service
) -> CandidateMetrics:
    return await service.metrics(job_id, candidate_id)


@router.get(
    "/{job_id}/evaluation",
    response_model=EvaluationReport,
    responses=problem_responses(404, 409),
    summary="Combined objective + model evaluation",
)
async def get_evaluation(job_id: JobIdPath, service: Service) -> EvaluationReport:
    return await service.evaluation(job_id)


@router.post(
    "/{job_id}/decision",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=RepairJob,
    responses=problem_responses(404, 409, 422),
    summary="Record the animator's choice",
    description=(
        "Allowed only in AWAITING_DECISION (otherwise 409 INVALID_STATE). Choosing A or B "
        "schedules the non-destructive apply (status APPLYING). REJECT_ALL moves the job to "
        "REJECTED. Choosing a FAILED candidate returns 422."
    ),
)
async def post_decision(job_id: JobIdPath, body: DecisionRequest, service: Service) -> RepairJob:
    return await service.decide(job_id, body)


@router.post(
    "/{job_id}/cancel",
    response_model=RepairJob,
    responses=problem_responses(404, 409),
    summary="Cancel a non-terminal job",
)
async def cancel_job(job_id: JobIdPath, service: Service) -> RepairJob:
    return await service.cancel(job_id)
