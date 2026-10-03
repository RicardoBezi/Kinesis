# ADR 0005: SQLite is the source of truth; Redis is an optional cache

**Status:** Accepted

## Context
The spec forbids using Redis as the permanent source of truth. The MVP runs on one workstation or one API instance.

## Decision
- **SQLite is permanent storage.** It uses the stdlib `sqlite3` module in WAL mode, called through `asyncio.to_thread`, behind a `JobStore` protocol.
  - It holds jobs, events, decisions and idempotency keys.
  - Idempotency keys have a UNIQUE constraint.
- **Redis is optional.** When it is configured, Redis (via `redis.asyncio`) holds four things:
  - the analysis cache;
  - the idempotency fast path;
  - per-job locks;
  - cross-worker DAG coordination.
- **In-process fallback.** If `KINESIS_REDIS_URL` is unset, or Redis returns an error, an in-process bounded LRU cache and an asyncio lock take its place. The fallback is logged.

## Consequences
- **No required services for local development.**
- **Flushing Redis only loses cache.**
- **Moving to Postgres later** means writing a new `JobStore` implementation. The API does not change.
