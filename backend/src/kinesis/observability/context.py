"""Log correlation (ARCHITECTURE §3): every record carries job, node, candidate and attempt.

The values live in ``ContextVar``s. asyncio copies the context into each task when it is
created, so a value bound around ``DagRunner.run`` reaches every node task, and a value bound
inside a node stays local to that node.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

job_id_var: ContextVar[str | None] = ContextVar("kinesis_job_id", default=None)
node_var: ContextVar[str | None] = ContextVar("kinesis_node", default=None)
candidate_var: ContextVar[str | None] = ContextVar("kinesis_candidate", default=None)
attempt_var: ContextVar[int | None] = ContextVar("kinesis_attempt", default=None)

_VARS: dict[str, ContextVar[Any]] = {
    "job_id": job_id_var,
    "node": node_var,
    "candidate": candidate_var,
    "attempt": attempt_var,
}


@contextmanager
def bind(**values: Any) -> Iterator[None]:
    """Set correlation fields for the enclosed block (and tasks created inside it)."""
    tokens = [(_VARS[k], _VARS[k].set(v)) for k, v in values.items()]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


class CorrelationFilter(logging.Filter):
    """Copies the correlation fields onto every record (``None`` when unset)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _VARS.items():
            if not hasattr(record, name):
                setattr(record, name, var.get())
        return True


class JsonFormatter(logging.Formatter):
    _STANDARD = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(
            {k: v for k, v in record.__dict__.items() if k not in self._STANDARD and v is not None}
        )
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Structured JSON logs on stderr for the ``kinesis`` logger tree (idempotent)."""
    root = logging.getLogger("kinesis")
    root.setLevel(level.upper())
    if not any(getattr(h, "_kinesis", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.addFilter(CorrelationFilter())
        handler._kinesis = True  # type: ignore[attr-defined]
        root.addHandler(handler)


__all__ = [
    "CorrelationFilter",
    "JsonFormatter",
    "attempt_var",
    "bind",
    "candidate_var",
    "configure_logging",
    "job_id_var",
    "node_var",
]
