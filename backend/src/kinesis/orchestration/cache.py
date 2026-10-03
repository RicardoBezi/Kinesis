"""Cache interface (ADR 0005). Values are JSON strings, so nothing is ever pickled.

The Redis implementation (Phase 3) uses ``redis.asyncio`` with one pool. When Redis
errors it degrades to a bounded in-process LRU and logs ``cache.degraded``. A cache
failure must never fail a job.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

from kinesis.schemas.candidate import canonical_json


@runtime_checkable
class Cache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_s: int) -> None: ...

    async def set_if_absent(self, key: str, value: str, *, ttl_s: int) -> bool:
        """Atomic SETNX. Used for idempotency fast paths and per-job locks."""
        ...

    async def delete(self, key: str) -> None: ...


def cache_key(namespace: str, *parts: object) -> str:
    """Build a deterministic, namespaced key: ``kinesis:<ns>:<sha256(canonical parts)>``.

    The parts must be JSON-serializable. Dict key order never changes the key.
    """
    if not namespace.isidentifier():
        raise ValueError(f"invalid cache namespace {namespace!r}")
    digest = hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return f"kinesis:{namespace}:{digest}"
