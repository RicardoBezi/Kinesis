"""Circuit breaker, one per provider endpoint (ARCHITECTURE §3, FAILURE_MODES #6).

CLOSED -> OPEN after ``failure_threshold`` consecutive counted failures; OPEN fails fast with
``CircuitOpen`` until ``recovery_s`` has passed; then HALF_OPEN admits at most
``half_open_max`` probe calls. A successful probe closes the breaker, a failed one re-opens it.

All state changes happen under one lock, and in-flight probes are counted explicitly and
released in ``finally``, so a cancelled or neutral probe cannot leak a slot. Client errors
(4xx other than 429, ``ProviderClientError``) are neutral: they neither count as failures nor
close the breaker.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

from kinesis.errors import CircuitOpen, ProviderClientError


class BreakerState(StrEnum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_s: float = 30.0,
        half_open_max: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or recovery_s < 0 or half_open_max < 1:
            raise ValueError("invalid breaker configuration")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self.half_open_max = half_open_max
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probes = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    async def _admit(self) -> bool:
        """Admit a call; returns True when the call is a half-open probe."""
        async with self._lock:
            if self._state is BreakerState.OPEN:
                elapsed = self._clock() - self._opened_at
                if elapsed < self.recovery_s:
                    raise CircuitOpen(
                        f"{self.name}: circuit open", retry_after_s=self.recovery_s - elapsed
                    )
                self._state = BreakerState.HALF_OPEN
                self._probes = 0
            if self._state is BreakerState.HALF_OPEN:
                if self._probes >= self.half_open_max:
                    raise CircuitOpen(f"{self.name}: half-open probe in flight", retry_after_s=1.0)
                self._probes += 1
                return True
            return False

    async def call[T](self, fn: Callable[[], Awaitable[T]]) -> T:
        probe = await self._admit()
        outcome: str = "neutral"
        try:
            value = await fn()
        except ProviderClientError:
            raise
        except Exception:
            outcome = "failure"
            raise
        else:
            outcome = "success"
            return value
        finally:
            async with self._lock:
                if probe:
                    self._probes = max(0, self._probes - 1)
                if outcome == "success":
                    self._state = BreakerState.CLOSED
                    self._failures = 0
                elif outcome == "failure":
                    self._failures += 1
                    if probe or self._failures >= self.failure_threshold:
                        self._state = BreakerState.OPEN
                        self._opened_at = self._clock()


__all__ = ["BreakerState", "CircuitBreaker"]
