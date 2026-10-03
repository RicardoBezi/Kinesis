"""RFC 7807 error handling. Every error response body is a ``Problem``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kinesis.schemas import ErrorCode, Problem

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ApiError(Exception):
    """An error raised by route handlers. It is rendered as a Problem response."""

    def __init__(self, status: int, code: ErrorCode, title: str, detail: str | None = None):
        super().__init__(title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


def not_implemented(phase: str) -> ApiError:
    return ApiError(501, ErrorCode.NOT_IMPLEMENTED, "Not implemented", f"Planned for {phase}")


def problem_response(problem: Problem) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
    )


def problem_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The OpenAPI ``responses`` entry declaring Problem bodies for the given statuses.

    422 is always included, because the validation handler renders a Problem in place of
    FastAPI's default HTTPValidationError.
    """
    return {
        s: {"model": Problem, "content": {PROBLEM_MEDIA_TYPE: {}}, "description": "Problem"}
        for s in sorted({*statuses, 422})
    }


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return problem_response(
            Problem(
                title=exc.title,
                status=exc.status,
                code=exc.code,
                detail=exc.detail,
                instance=request.url.path,
            )
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = tuple(
            {"loc": ".".join(str(p) for p in e.get("loc", ())), "msg": str(e.get("msg", ""))}
            for e in exc.errors()[:20]
        )
        return problem_response(
            Problem(
                title="Request validation failed",
                status=422,
                code=ErrorCode.VALIDATION_ERROR,
                instance=request.url.path,
                errors=errors,
            )
        )
