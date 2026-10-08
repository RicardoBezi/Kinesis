"""Artifact download. Files are resolved by id through the store, never by client path."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import FileResponse

from kinesis.api.deps import get_service
from kinesis.api.errors import problem_responses
from kinesis.jobs.service import JobService

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])


@router.get(
    "/{artifact_id}",
    response_class=FileResponse,
    responses={
        200: {
            "content": {
                "image/jpeg": {},
                "application/json": {},
                "application/octet-stream": {},
            },
            "description": "Artifact bytes",
        },
        **problem_responses(404),
    },
    summary="Download an artifact (or one frame of a frame sequence)",
)
async def get_artifact(
    artifact_id: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{5,63}$")],
    service: Annotated[JobService, Depends(get_service)],
    frame: Annotated[int | None, Query(ge=0, le=10_000)] = None,
) -> FileResponse:
    path, media_type = await service.artifact(artifact_id, frame)
    return FileResponse(path, media_type=media_type)
