# ADR 0006: Progress via a polled, sequenced event log

**Status:** Accepted

## Context
The client must show job progress and recover after a disconnect. WebSockets and SSE add reconnect logic and are harder to test.

## Decision
- **Every state change is an event.** Each job state change appends a `JobEvent` with a per-job `seq` that only increases. The pair `(job_id, seq)` is unique.
- **The client polls.** While a job is active, the client polls `GET /v1/jobs/{id}` and `GET /v1/jobs/{id}/events?after_seq=N` every 1 s. It backs off when the job is idle.
- **Duplicates are dropped.** The client deduplicates events by `(job_id, seq)`.

## Consequences
- **Reconnecting is trivial.** The client resumes from the last `seq` it saw.
- **Easy to test.** Ktor MockEngine can simulate the whole exchange.
- **SSE can come later.** It can be layered on top without breaking this contract.
