# Phase 5: review client

**Status: complete (2026-10-08).** The Compose Desktop client covers steps 10–12 of the definition of done:
- it shows Original, A and B side by side, synchronized frame by frame;
- the animator selects a candidate;
- the selected repair is applied non-destructively.

The client was checked against a live server running real Blender. The screenshot below is an off-screen render of the real client for a real job on the canonical fixture, not a mock-up.

![Kinesis review client](img/review-client.png)

The job above, with the mock provider supplying the visual scores, went from upload to `AWAITING_DECISION` in **8.1 s**:
- headless extraction;
- two candidates applied and rendered in parallel;
- the original rendered with the same camera;
- measurement and ranking.

## Deliverables

| Deliverable | Where |
|---|---|
| `GET /v1/stats/product`: acceptance, model/human agreement, selected slip reduction, time to decision, cost per accepted repair | [`jobs/stats.py`](../backend/src/kinesis/jobs/stats.py) |
| `GET /v1/jobs`: newest first, paged with `before` | [`api/routes_jobs.py`](../backend/src/kinesis/api/routes_jobs.py) |
| Per-job model cost (SQLite `job_costs`) | [`storage/sqlite.py`](../backend/src/kinesis/storage/sqlite.py) |
| Typed Ktor API client (Problem → `ApiException`) | [`client/.../api/KinesisApi.kt`](../client/kotlin-desktop/src/main/kotlin/dev/kinesis/client/api/KinesisApi.kt) |
| Pure state reducer: phases, A/B selection, shared frame index, playback, errors | [`client/.../state/ReviewState.kt`](../client/kotlin-desktop/src/main/kotlin/dev/kinesis/client/state/ReviewState.kt) |
| Resumable event poller (ADR 0006) | [`client/.../state/JobPoller.kt`](../client/kotlin-desktop/src/main/kotlin/dev/kinesis/client/state/JobPoller.kt) |
| UI: job list and health, New repair dialog, synced previews, metrics, recommendation, decision | [`client/.../ui/`](../client/kotlin-desktop/src/main/kotlin/dev/kinesis/client/ui/), [`Main.kt`](../client/kotlin-desktop/src/main/kotlin/dev/kinesis/client/Main.kt) |
| Off-screen screenshot harness | `gradlew screenshot -Pjob=<id> -Papi=<url>` ([`Screenshot.kt`](../client/kotlin-desktop/src/test/kotlin/dev/kinesis/client/Screenshot.kt)) |

## Tests

- **Kotlin**, `gradlew test`:
  - generated-model serialization;
  - the reducer, covering every `JobStatus` (with a guard that fails if a new status is added unmapped);
  - selection rules: the recommendation is preselected, a failed candidate cannot be chosen, and a refresh keeps a valid choice;
  - event de-duplication;
  - frame clamping and playback wrap;
  - error states rendered from a Problem body;
  - the poller resuming from `next_seq` after a simulated disconnect, using Ktor `MockEngine`;
  - idle backoff measured in virtual time;
  - the `Idempotency-Key` header;
  - transport errors.
- **Python:** a pure stats test, plus an API test covering the job list and product stats after a decision.

## Design decisions

1. **The client is generated from the contract.** Every model comes from `docs/api/openapi.json` (ADR 0008), so a backend schema change breaks the client build rather than the user.
2. **All UI state goes through one pure reducer.** That makes every rule testable without a window. Writing those tests exposed a real bug: the first `JobLoaded` was treated as a job switch and dropped the events that had just arrived.
3. **One shared frame index drives all three panes.** It is bounded by the shortest sequence, so the panes can never drift apart. Playback runs at 12 fps, and decoded frames are kept in an LRU of 600.
4. **The recommendation is preselected but never applied automatically.** The animator must press *Apply candidate X*. `time_to_decision_s` is measured from the moment the job first reached review.
5. **Cost per accepted repair is `null` whenever any model call had no known price.** The mock provider has no prices, for example. This means the metric is never silently understated.
6. **`GET /v1/jobs` is a new route.** The review client needs a job list, and API.md documents it.

## Running it

```bash
uv run task run                                   # API on :8000 (needs KINESIS_BLENDER_BIN for real jobs)
uv run task client                                # opens the review window (KINESIS_API_URL overrides the URL)
```
In the window, choose **New repair…**, then `blender/fixtures/foot_slide_v1.blend`, `foot.L`, frames 45–90, and **Repair**.
