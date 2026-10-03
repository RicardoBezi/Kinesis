"""Artifact download. Files are resolved by id through the store, never by client path."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query
from fastapi.responses import FileResponse

from kinesis.api.errors import not_implemented, problem_responses

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
        **problem_responses(404, 501),
    },
    summary="Download an artifact (or one frame of a frame sequence)",
)
async def get_artifact(
    artifact_id: Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{5,63}$")],
    frame: Annotated[int | None, Query(ge=0, le=10_000)] = None,
) -> FileResponse:
    raise not_implemented("Phase 2")
