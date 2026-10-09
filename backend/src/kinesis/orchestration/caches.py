"""Cache implementations (ADR 0005): an in-process LRU and Redis with graceful degradation."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from kinesis.observability.metrics import CACHE_REQUESTS

log = logging.getLogger("kinesis.cache")


class MemoryCache:
    """Bounded LRU with per-entry TTL. Single event loop, so no lock is needed."""

    def __init__(
        self, max_entries: int = 1024, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max = max_entries
        self._clock = clock

    def _live(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires <= self._clock():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    async def get(self, key: str) -> str | None:
        return self._live(key)

    async def set(self, key: str, value: str, *, ttl_s: int) -> None:
        self._data[key] = (value, self._clock() + ttl_s)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    async def set_if_absent(self, key: str, value: str, *, ttl_s: int) -> bool:
        if self._live(key) is not None:
            return False
        await self.set(key, value, ttl_s=ttl_s)
        return True

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


class RedisCache:
    """``redis.asyncio`` with one pool. Any Redis error degrades to ``fallback`` for that call;
    ``cache.degraded`` is logged at most once per ``log_every_s``. A cache failure never fails a
    job (FAILURE_MODES #16)."""

    def __init__(
        self,
        client: Any,
        *,
        fallback: MemoryCache | None = None,
        log_every_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.fallback = fallback or MemoryCache()
        self._log_every_s = log_every_s
        self._clock = clock
        self._last_log = -float("inf")
        self.degraded_calls = 0

    @classmethod
    def from_url(cls, url: str, **kw: Any) -> RedisCache:
        import redis.asyncio as redis

        return cls(redis.Redis.from_url(url, decode_responses=True), **kw)

    async def _try[T](
        self, op: Callable[[], Awaitable[T]], fallback: Callable[[], Awaitable[T]]
    ) -> T:
        from redis.exceptions import RedisError

        try:
            return await op()
        except (RedisError, OSError, TimeoutError) as exc:
            self.degraded_calls += 1
            CACHE_REQUESTS.labels("redis", "degraded").inc()
            now = self._clock()
            if now - self._last_log >= self._log_every_s:
                self._last_log = now
                log.warning("cache.degraded", extra={"error": type(exc).__name__})
            return await fallback()

    async def get(self, key: str) -> str | None:
        async def op() -> str | None:
            value = await self._client.get(key)
            return None if value is None else str(value)

        return await self._try(op, lambda: self.fallback.get(key))

    async def set(self, key: str, value: str, *, ttl_s: int) -> None:
        async def op() -> None:
            await self._client.set(key, value, ex=ttl_s)

        await self._try(op, lambda: self.fallback.set(key, value, ttl_s=ttl_s))

    async def set_if_absent(self, key: str, value: str, *, ttl_s: int) -> bool:
        async def op() -> bool:
            return bool(await self._client.set(key, value, ex=ttl_s, nx=True))

        return await self._try(op, lambda: self.fallback.set_if_absent(key, value, ttl_s=ttl_s))

    async def delete(self, key: str) -> None:
        async def op() -> None:
            await self._client.delete(key)

        await self._try(op, lambda: self.fallback.delete(key))


__all__ = ["MemoryCache", "RedisCache"]
