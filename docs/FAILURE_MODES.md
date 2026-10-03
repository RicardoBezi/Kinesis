# Kinesis: Failure-Mode Matrix

There are three rules:
1. **Isolate.** A failure is contained to the smallest scope that is still correct: one call, then one node, then one candidate, and only then the whole job.
2. **Retry only what can succeed on a second try.** Validation errors and deterministic output errors are never retried.
3. **Every failure the user sees carries a machine-readable `ErrorCode`** and a message naming the DAG node.

Retry policies use exponential backoff with full jitter (base 0.5 s, cap 8 s); see [ARCHITECTURE §3](ARCHITECTURE.md#3-dag-definition). Breakers exist one per provider endpoint, with defaults of 5 consecutive failures to open, 30 s recovery, and 1 half-open probe.

| # | Failure | Detection | Retry? | Isolation scope | Job / candidate outcome | Code |
|---|---|---|---|---|---|---|
| 1 | Model timeout (planning) | `httpx.TimeoutException` → `ProviderTimeout` | 2× | `plan_repair` node | Fallback plan is used; job continues; `PLAN_REJECTED` event with reason | `PROVIDER_TIMEOUT` (recorded on the event) |
| 2 | HTTP 429 | status 429 → `ProviderRateLimited(retry_after)` | 2×, waiting at least `Retry-After` | call | Same as #1 if retries are exhausted | `PROVIDER_RATE_LIMITED` |
| 3 | HTTP 5xx | → `ProviderServerError` | 2× | call | Same as #1 | `PROVIDER_UNAVAILABLE` |
| 4 | HTTP 4xx (other) | → `ProviderClientError` | no; not a breaker failure | call | Same as #1; logged as a configuration error | `PROVIDER_UNAVAILABLE` |
| 5 | Malformed, empty, unsupported or hallucinated plan | `accept_model_plan` rejects it | no | `plan_repair` | Fallback plan; rejection reasons stored in `plan.fallback_reason` | `PLAN_INVALID` (event) |
| 6 | Breaker open | `CircuitOpen` raised before the call | no (fails fast) | call | Planning: fallback. Evaluation: `DEGRADED` | `PROVIDER_UNAVAILABLE` |
| 7 | Visual evaluation fails or is invalid for one candidate | provider result INVALID or UNAVAILABLE | per-call 2× (transport only) | that candidate's evaluation | `evaluator_status=DEGRADED`; objective ranking still shown | none (report field) |
| 8 | Blender crash (non-zero exit, signal) | runner exit code; missing result | `extract`: 2×; `apply_render`: 1× | node | `extract` exhausted: job FAILED. `apply_render`: that candidate FAILED | `WORKER_CRASHED` |
| 9 | Blender timeout | `asyncio.wait_for` on the process, then kill the process tree | as #8 | node | as #8 | `WORKER_TIMEOUT` |
| 10 | Malformed worker result | Pydantic validation of `result.json` fails, or `ok:false` | no (deterministic) | node | as #8; result file kept as a LOG artifact | `WORKER_OUTPUT_INVALID` |
| 11 | Render fails or times out, keys applied fine | worker reports a render error | 1× | candidate | Candidate FAILED; sibling continues | `RENDER_FAILED` |
| 12 | One candidate fails, the other succeeds | `evaluate` has `join=ANY_SUCCESS` | n/a | candidate | Job → `AWAITING_DECISION`; failed candidate shown with error; the decision cannot pick it (422) | per candidate |
| 13 | Both candidates fail | `evaluate` is SKIPPED | n/a | job | Job → FAILED | `NO_VIABLE_CANDIDATE` |
| 14 | Original action hash changes | `original_action_hash_before != _after` | no | candidate | Candidate FAILED. This is a **bug alarm** and is logged at ERROR | `WORKER_OUTPUT_INVALID` |
| 15 | IK target unreachable | the solver clamps the target | n/a | metric | Not fatal; `ik_unreachable_frames > 0` is shown, and the golden threshold is 0 | none |
| 16 | Redis unavailable | connection or timeout error in the cache | no | cache | Degrades to the in-process LRU; logs `cache.degraded` once per minute; job unaffected | none |
| 17 | SQLite busy or locked | `sqlite3.OperationalError: database is locked` | 3× with short backoff | store call | Persistent failure: job FAILED | `STORAGE_ERROR` |
| 18 | Disk full while writing artifacts | `OSError(ENOSPC)` | no | node | Node FAILED, so the candidate or job fails | `STORAGE_ERROR` |
| 19 | Serverless Job submit or poll fails (Phase 6) | Nebius API error or job state FAILED | submit: 2×; job failure: as #8 | node | as #8 | `WORKER_CRASHED` |
| 20 | Duplicate status callback or event | unique `(job_id, dedupe_key)` | n/a | event | Dropped silently (counted in metrics) | none |
| 21 | Client disconnects mid-job | n/a (server is stateless per request) | n/a | none | Client resumes from `after_seq` | none |
| 22 | Invalid upload (not a .blend, or too big) | magic bytes / size check, enforced while streaming | no | request | 422 / 413 | `FILE_INVALID` / `FILE_TOO_LARGE` |
| 23 | Path traversal attempt | ids validated by regex; paths resolved and checked with `is_relative_to(jobs_root)` | no | request | 404 / 422; logged at WARNING | `VALIDATION_ERROR` |
| 24 | `.blend` contains auto-run scripts | worker runs with `--factory-startup` and asserts auto-exec is off | no | job | Scripts never run; inspect reports `autoexec_disabled=true` | none |
| 25 | Job cancelled mid-run | `POST /cancel` | n/a | job | All running node tasks are cancelled in `finally`; worker processes killed; status CANCELLED | none |
| 26 | API process restarts mid-job | jobs found RUNNING at startup | n/a | job | Marked FAILED with "interrupted" (MVP); resuming is a post-MVP feature | `INTERNAL` |

## Error message standard

```
<node>: <what failed> (<cause>); <what Kinesis did about it>
e.g. "apply_render_B: Blender exited with code -11 after 12.4s (attempt 2/2); candidate B marked FAILED, candidate A unaffected"
```

The full stderr from the worker is stored as a `LOG` artifact. It is never inlined into API error messages.
