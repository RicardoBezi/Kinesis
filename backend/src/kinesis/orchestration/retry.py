"""Call-level retry for provider calls (FAILURE_MODES #1-#4, #6).

Node-level retries live in the DAG engine. Provider calls need retries *inside* a node,
because exhausting them must fall back (deterministic plan, DEGRADED evaluation) rather than
fail the node. Same policy and jitter rules as the engine.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from kinesis.orchestration.dag import RetryPolicy


async def call_with_retry[T](
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException, float], Awaitable[None]] | None = None,
) -> T:
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except Exception as exc:
            if attempt >= policy.max_attempts or not policy.classify(exc):
                raise
            delay = jitter() * policy.max_delay(attempt)
            retry_after = getattr(exc, "retry_after_s", None)
            if isinstance(retry_after, int | float):
                delay = max(delay, float(retry_after))
            if on_retry is not None:
                await on_retry(attempt, exc, delay)
            await sleep(delay)


__all__ = ["call_with_retry"]
