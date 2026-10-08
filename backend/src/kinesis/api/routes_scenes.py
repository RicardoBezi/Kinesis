"""Scene upload and inspection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, UploadFile, status

from kinesis.api.deps import get_service
from kinesis.api.errors import problem_responses
from kinesis.jobs.service import JobService
from kinesis.schemas import SceneRef

router = APIRouter(prefix="/v1/scenes", tags=["scenes"])

SceneIdPath = Annotated[str, Path(pattern=r"^[a-z0-9][a-z0-9_-]{5,63}$")]
Service = Annotated[JobService, Depends(get_service)]
CHUNK = 1 << 20


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(CHUNK):
        yield chunk


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SceneRef,
    responses=problem_responses(413, 422, 501),
    summary="Upload a .blend copy and inspect it",
    description=(
        "Accepts a single `.blend` file (at most KINESIS_MAX_UPLOAD_MB; it must start with "
        "the `BLENDER` magic bytes). The client filename is ignored. The file is stored as "
        "`input/scene.blend` in a new scene directory and inspected headlessly with "
        "auto-run scripts disabled."
    ),
)
async def upload_scene(file: Annotated[UploadFile, File()], service: Service) -> SceneRef:
    return await service.upload_scene(_chunks(file))


@router.get(
    "/{scene_id}",
    response_model=SceneRef,
    responses=problem_responses(404, 501),
    summary="Get an uploaded scene's inventory",
)
async def get_scene(scene_id: SceneIdPath, service: Service) -> SceneRef:
    return await service.get_scene(scene_id)
