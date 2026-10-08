# Phase 3: orchestration

**Status: complete (2026-10-08).** Each job runs as a DAG with these properties:
- candidates A and B run concurrently;
- a failure is isolated to the smallest correct scope (a call, a node or a candidate);
- every user-visible failure carries a machine-readable code and a message naming the node.

All 13 scenarios in the failure-mode test list are live, except the serverless one, which waits for Phase 6. Every job route works behind the API.

The same flow also runs end to end against real Blender, as a `blender`-marked test:
1. upload the fixture;
2. run the repair DAG (detecting the 10 cm slide, rendering A and B, measuring, recommending B);
3. choose B;
4. download the exported `.blend`.

## Deliverables

| Deliverable | Where |
|---|---|
| DAG engine: concurrency, explicit edges, SKIPPED / ANY_SUCCESS, `SkipNode`, full-jitter retries, `Retry-After`, timeouts, cancellation, isolated callbacks, JSON caching | [`orchestration/engine.py`](../backend/src/kinesis/orchestration/engine.py) |
| Circuit breaker (CLOSED / OPEN / HALF_OPEN under one lock, explicit probe accounting) | [`orchestration/breaker.py`](../backend/src/kinesis/orchestration/breaker.py) |
| In-process LRU and Redis caches; Redis degrades per call | [`orchestration/caches.py`](../backend/src/kinesis/orchestration/caches.py) |
| SQLite job and artifact stores (WAL, idempotency, sequenced and de-duplicated events, id-addressed artifacts) | [`storage/sqlite.py`](../backend/src/kinesis/storage/sqlite.py) |
| The per-job repair DAG | [`jobs/pipeline.py`](../backend/src/kinesis/jobs/pipeline.py) |
| JobService: upload, validation, runs, decision/export, cancel, restart recovery | [`jobs/service.py`](../backend/src/kinesis/jobs/service.py) |
| API routes and service wiring | [`api/`](../backend/src/kinesis/api/) |
| MockProvider and FakeJobRunner (scripted faults) | [`providers/mock.py`](../backend/src/kinesis/providers/mock.py), [`testing/fake_runner.py`](../backend/src/kinesis/testing/fake_runner.py) |
| Log correlation (`job_id`, `node`, `candidate`, `attempt`) and Prometheus metrics | [`observability/`](../backend/src/kinesis/observability/) |
| Tests | `tests/unit/test_engine.py`, `test_storage.py`, `tests/fault/`, `tests/integration/test_api_jobs.py`, and the end-to-end test in `test_blender_worker.py` |

## The DAG as built

```
validate_input → extract_scope → {contact_analysis, motion_analysis, constraint_check} → defect_report → plan_repair
plan_repair → generate_A → apply_render_A → metrics_A ┐
plan_repair → generate_B → apply_render_B → metrics_B ├→ evaluate (ANY_SUCCESS) → AWAITING_DECISION
plan_repair → render_original ─────────────────────────┘
decision: apply_selected (export) → COMPLETED
```

When nothing slides (severity NONE), `defect_report` raises `SkipNode`. The job then ends as COMPLETED with no candidates.

## Design decisions and changes

1. **A new `render_original` node.** The A/B review needs the original rendered with the same crop camera, so the DAG renders it in parallel with the candidates. The frames are stored in the new field `RepairJob.original_artifacts`, which defaults to empty. OpenAPI and the Kotlin samples were regenerated, and the generated Kotlin models still pass their tests.
2. **Provider retries happen at call level, inside the node.** When `plan_repair` exhausts its retries it must fall back to the presets, not fail. So provider calls use `call_with_retry` behind the endpoint's breaker. Worker nodes keep node-level retries: `extract` gets 2 retries, `apply_render` gets 1.
3. **`evaluate` joins `metrics_A`, `metrics_B` and `render_original` with ANY_SUCCESS.** It skips itself when no candidate succeeded. The job then fails with `NO_VIABLE_CANDIDATE`.
4. **`evaluator_status` rules.** A null provider gives SKIPPED, and no call is made. Missing original frames, or any failed or invalid call, gives DEGRADED. The objective ranking is always present. Each vision call sends 5 original and 5 candidate frames, under the S4 limit of 10.
5. **Recommendation is objective-only for now.** It names the best ungated objective score. On the fixture that is B (0.76 against 0.69). Phase 4 adds the model's view.
6. **One lock per job for the aggregate.** Concurrent candidate branches update `RepairJob` through `JobContext.mutate`. Status and `last_event_seq` live in their own SQLite columns, so a stale `save_job` cannot roll them back.
7. **SQLite runs with `synchronous=NORMAL`** (WAL). A crash cannot corrupt the database, but the last few commits can be lost on power failure. FULL fsync'd every event append.
8. **Cancellation kills Blender.** `LocalJobRunner` now kills the subprocess when its task is cancelled, not only on timeout.
9. **Uploads are spooled by Starlette before the copy.** The size cap and magic check run while Kinesis copies the upload into the scene directory. True request-body streaming is a hardening item for Phase 7.
10. **`KINESIS_PROVIDER=token_factory` falls back to the null provider** until Phase 4, with a warning.
11. **Two error-code choices** that API.md left open: choosing a FAILED candidate returns `422 VALIDATION_ERROR`, and a scene that fails inspection returns `422 FILE_INVALID`.

## Numbers

- **Tests:** the offline suite has 333 tests and takes about 41 s; the Blender suite has 14 tests and takes about 29 s locally.
- **Speed of one job on the fake runner:** about 1 s, mostly thread-pool hops for about 100 small SQLite calls on Windows. Batching event writes is an option if this starts to matter.

## Carried forward

- **Phase 4:** implement `TokenFactoryProvider` and plug it into the existing provider seam and breakers. Turn on `/v1/health/providers` and the transport replay tests.
- **Phase 5:** the Compose client consumes the routes as they are now, including `original_artifacts` for the A/B view. Add `/v1/stats/product`.
- **Phase 7:** true streaming uploads, and per-job workspace cleanup with the TTL sweeper on a schedule.
