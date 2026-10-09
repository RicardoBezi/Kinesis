"""Nebius AI Jobs REST client (docs.nebius.com/rest-api/ai/v1/jobs).

| call | endpoint |
|---|---|
| create | ``POST /ai/v1/jobs`` -> an Operation whose ``resourceId`` is the job id |
| get | ``GET /ai/v1/jobs/{id}`` -> ``status.state`` and timestamps |
| cancel | ``POST /ai/v1/jobs/{id}:cancel`` |

Errors are classified for the runner: 401 refreshes the token once and retries; 429, 5xx,
timeouts and network errors are retryable ``NebiusApiError``; other 4xx are not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import httpx

from kinesis.errors import KinesisError
from kinesis.jobs.nebius.auth import NebiusTokenProvider
from kinesis.schemas.common import ErrorCode

API_BASE_URL = "https://api.nebius.cloud/"


class NebiusApiError(KinesisError):
    code = ErrorCode.WORKER_CRASHED

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class JobState(StrEnum):
    STATE_UNSPECIFIED = "STATE_UNSPECIFIED"
    PROVISIONING = "PROVISIONING"
    STARTING = "STARTING"
    IMAGE_PULLING = "IMAGE_PULLING"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    DELETING = "DELETING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.ERROR)


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    state: JobState
    message: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class NebiusJobsClient:
    def __init__(
        self,
        tokens: NebiusTokenProvider,
        *,
        base_url: str = API_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._tokens = tokens
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
            headers={"User-Agent": "kinesis/0.1"},
        )

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self._tokens.token()
            try:
                response = await self._http.request(
                    method, path, headers={"Authorization": f"Bearer {token}"}, **kw
                )
            except httpx.TimeoutException:
                raise NebiusApiError(f"{method} {path}: timed out", retryable=True) from None
            except httpx.TransportError as exc:
                raise NebiusApiError(
                    f"{method} {path}: {type(exc).__name__}", retryable=True
                ) from None
            if response.status_code == 401 and attempt == 1:
                self._tokens.invalidate()  # expired or revoked: exchange again, once
                continue
            if response.status_code >= 400:
                retryable = response.status_code == 429 or response.status_code >= 500
                raise NebiusApiError(
                    f"{method} {path}: HTTP {response.status_code} {_message(response)}",
                    retryable=retryable,
                    status=response.status_code,
                )
            data = response.json() if response.content else {}
            return data if isinstance(data, dict) else {}
        raise NebiusApiError(f"{method} {path}: unauthorized after token refresh", retryable=False)

    async def create_job(self, body: dict[str, Any]) -> str:
        operation = await self._request("POST", "ai/v1/jobs", json=body)
        job_id = operation.get("resourceId")
        if not job_id:
            raise NebiusApiError("create returned no resourceId", retryable=False)
        return str(job_id)

    async def get_job(self, job_id: str) -> JobStatus:
        data = await self._request("GET", f"ai/v1/jobs/{job_id}")
        status = data.get("status") or {}
        details = status.get("stateDetails") or {}
        try:
            state = JobState(status.get("state", "STATE_UNSPECIFIED"))
        except ValueError:
            state = JobState.STATE_UNSPECIFIED
        return JobStatus(
            job_id=job_id,
            state=state,
            message=str(details.get("message") or details.get("code") or "")[:500],
            started_at=_ts(status.get("startedAt")),
            finished_at=_ts(status.get("finishedAt")),
        )

    async def cancel_job(self, job_id: str) -> None:
        await self._request("POST", f"ai/v1/jobs/{job_id}:cancel")

    async def aclose(self) -> None:
        await self._http.aclose()


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or "")[:200]
    return ""


__all__ = ["API_BASE_URL", "JobState", "JobStatus", "NebiusApiError", "NebiusJobsClient"]
