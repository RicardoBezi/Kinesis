"""Pipeline exception hierarchy. Every exception says whether it is retryable.

The retry policy (orchestration) retries only ``KinesisError`` subclasses whose
``retryable`` flag is True. Validation errors and other unknown exceptions are never
retried. This fixes Evoco's retry-on-anything behaviour.
"""

from __future__ import annotations

from kinesis.schemas.common import ErrorCode, ErrorInfo


class KinesisError(Exception):
    code: ErrorCode = ErrorCode.INTERNAL
    retryable: bool = False

    def __init__(self, message: str, *, node: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.node = node

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(
            code=self.code, message=self.message[:2000], node=self.node, retryable=self.retryable
        )


# ---------------------------------------------------------------- provider


class ProviderError(KinesisError):
    code = ErrorCode.PROVIDER_UNAVAILABLE


class ProviderTimeout(ProviderError):
    code = ErrorCode.PROVIDER_TIMEOUT
    retryable = True


class ProviderRateLimited(ProviderError):
    code = ErrorCode.PROVIDER_RATE_LIMITED
    retryable = True

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kw: str) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s


class ProviderServerError(ProviderError):
    """HTTP 5xx."""

    retryable = True


class ProviderClientError(ProviderError):
    """HTTP 4xx other than 429. Not retryable, and it does not count as a breaker failure."""


class CircuitOpen(ProviderError):
    """Fail fast: the breaker is open. Retrying immediately cannot succeed."""

    def __init__(self, message: str, *, retry_after_s: float, **kw: str) -> None:
        super().__init__(message, **kw)
        self.retry_after_s = retry_after_s


# ---------------------------------------------------------------- worker


class WorkerCrashed(KinesisError):
    """The Blender process exited non-zero or was killed."""

    code = ErrorCode.WORKER_CRASHED
    retryable = True


class WorkerTimeout(KinesisError):
    code = ErrorCode.WORKER_TIMEOUT
    retryable = True


class WorkerOutputInvalid(KinesisError):
    """result.json was missing, malformed or failed schema validation. Deterministic, so
    it is not retried."""

    code = ErrorCode.WORKER_OUTPUT_INVALID


class RenderFailed(KinesisError):
    code = ErrorCode.RENDER_FAILED


# ---------------------------------------------------------------- selection


class SelectionInvalid(KinesisError):
    """The selection does not fit the scene: unknown bone, bad frame range, unsupported rig.

    Deterministic, so never retried. The API renders it as a 4xx Problem with ``code``.
    """

    def __init__(self, code: ErrorCode, message: str, *, node: str | None = None) -> None:
        super().__init__(message, node=node)
        self.code = code


# ---------------------------------------------------------------- pipeline


class NoViableCandidate(KinesisError):
    code = ErrorCode.NO_VIABLE_CANDIDATE


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, KinesisError) and exc.retryable
