# Phase 6: serverless

**Status: implementation complete (2026-10-09); live Nebius runs pending (account suspended).**

**Done:** the containerized worker, and a `ContainerJobRunner` that runs it, are complete. The container gives **the same golden results as host Blender**, which a test checks on every nightly CI run.

**Nebius Serverless Jobs:** two runners submit the same container to Nebius. `NebiusJobRunner` runs one Nebius job per worker step; `NebiusSessionRunner` runs one per Kinesis job. Both are implemented and contract-tested against the Nebius Serverless Jobs API. **Live runs are pending**, because the owner's Nebius AI Cloud account is suspended (concern C2). See [Nebius runners](#nebius-runners-implemented-and-contract-tested-live-runs-pending).

## Done

| Deliverable | Where |
|---|---|
| Worker image: Ubuntu 24.04, checksum-verified Blender 4.5.14, Mesa software EGL, unprivileged, fixed entrypoint argv | [`infra/docker/blender-worker.Dockerfile`](../infra/docker/blender-worker.Dockerfile), `uv run task worker-image` |
| `ContainerJobRunner`: one `docker run` per worker command | [`jobs/container.py`](../backend/src/kinesis/jobs/container.py) |
| Runner selection: `KINESIS_JOB_RUNNER=local\|container\|nebius`, with `KINESIS_WORKER_IMAGE` | [`api/deps.py`](../backend/src/kinesis/api/deps.py) |
| Parity test: a whole job in the container against the golden snapshot, including the export | [`tests/integration/test_container_worker.py`](../tests/integration/test_container_worker.py), `uv run task test-container`, a nightly CI job |

`ContainerJobRunner` isolates each run:
- no network;
- a read-only root filesystem;
- a tmpfs for Blender's config;
- the job directory as the only mount, and that mount is the only data the worker sees;
- `no-new-privileges`, with CPU and memory caps.

The CPU cap defaults to `min(4, host CPUs)`, because Docker rejects a larger cap (hosted runners have 2). When `docker run` itself fails (exit 125–127), Docker's one-line reason goes into the error message.

On cancel or timeout the runner force-removes the container. Killing only the `docker` CLI would leave Blender running. This is the same process boundary a serverless job has, so the image is what Nebius would run.

### Local versus container (workstation, 2026-10-09)

| | Upload → review | Notes |
|---|---|---|
| Host Blender (`LocalJobRunner`) | 8.1 s | Renders on the GPU (Workbench, about 0.06 s/frame) |
| Container (`ContainerJobRunner`, 4 CPUs) | 31.0 s | Renders on the CPU through Mesa llvmpipe; the GitHub CPU runner measured about 0.42 s/frame in S3 |
| Container start-up overhead | about 0.45 s per worker command | `docker run` + `blender --version`: 0.55–0.62 s, against 0.12–0.14 s on the host |
| Container on GitHub CI (2 CPUs) | 61.5 s | Same golden results; the nightly `container` job in `blender.yml` |

A job runs 6 worker commands, so start-up adds about 3 s. The rest of the gap comes from rendering about 250 frames (the A, B and original crops plus context frames) on the CPU. **What this means for Nebius:**
- a GPU instance type would recover most of that time;
- on CPU-only shapes, rendering fewer preview frames (for example every 2nd frame) is the main lever.

Either way, the results are identical.

## Nebius runners: implemented and contract-tested; live runs pending

**Status (2026-10-09):** both Nebius runner designs are **implemented and contract-tested against the Nebius Serverless Jobs API**, using a fake API that follows the documented contract. **Live runs are pending:** the owner's Nebius AI Cloud account is suspended (payment failed), so there is no project, registry, bucket or service account. None of the cloud timings below are measured; they are marked **EXPECTED**.

### The API contract followed (docs.nebius.com, verified 2026-10-09)

**Authentication**
- A service-account authorized key signs an RS256 JWT: `kid` is the key id, `iss` and `sub` are the service account, and the JWT lives 5 minutes.
- The JWT is exchanged with RFC 8693 at `https://auth.eu.nebius.com/oauth2/token/exchange` for a bearer token valid 12 hours.
- Tokens are cached and refreshed early. A 401 triggers exactly one re-exchange.

**Jobs REST API**

| Call | Endpoint | Notes |
|---|---|---|
| Create | `POST https://api.nebius.cloud/ai/v1/jobs` | Returns an Operation whose `resourceId` is the job id |
| Poll | `GET /ai/v1/jobs/{id}` | `status.state` moves through PROVISIONING → STARTING → IMAGE_PULLING → RUNNING → COMPLETED / FAILED / CANCELLED / ERROR |
| Cancel | `POST /ai/v1/jobs/{id}:cancel` | |

Completion is signalled by **polling only**.

**Job spec**
- `args` is a single string, `--command <enum> --spec /work/...`, under the image's fixed entrypoint.
- `timeout` is `"3600s"`; that is the Nebius minimum.
- `disk` is `{type: NETWORK_SSD, sizeBytes}`. It is set to 32 GiB; the default is 250 GiB, and disk is billed.
- `restartAttempts` is `"0"`.
- `pricingModel` is on-demand, or `followsSpotPrice` when preemptible.

**Job I/O**
- The job's bucket prefix is mounted as a volume at `/work`, with `source` = bucket, `sourcePath` = `kinesis/jobs/<job_id>/`, `READ_WRITE`, and `s3Config` pointing at `https://storage.us-central1.nebius.cloud`.
- The worker's job-directory contract is therefore unchanged.
- The API side uploads inputs and downloads outputs with boto3 (the `nebius` extra).

### Design A vs design B

| | A: `NebiusJobRunner` (default, `NEBIUS_RUNNER_MODE=job`) | B: `NebiusSessionRunner` (`NEBIUS_RUNNER_MODE=session`) |
|---|---|---|
| Nebius jobs per Kinesis job | One per worker step (about 7) | One per Kinesis job, plus one per upload inspect and one per export |
| What runs in the container | The worker, with the same fixed argv as locally | `blender/worker/session.py`: watches `/work/queue/`, runs each request with the same fixed argv, writes `<id>.done.json`, and stops on `queue/STOP` or after 10 min idle |
| Start-up cost | **EXPECTED:** paid on every step (a fresh VM plus a 2 GB image pull; image caching is not documented) | **EXPECTED:** paid once per Kinesis job |
| Idle cost | None | **EXPECTED:** the VM idles between steps, bounded by the idle timeout |
| Risks | Many provisioning waits | Relies on the bucket mount making new objects visible to the container promptly; **unverified** |
| Recommendation | Start here: stateless and the simplest to debug | Switch once live runs show that per-step start-up dominates |

Both designs share these safeguards:
- **Watchdog.** A client-side watchdog (`NEBIUS_WATCHDOG_S`, default 900 s) cancels a job that runs too long, because Nebius' own minimum timeout is 1 h.
- **Budget.** A `SpendGuard` (`NEBIUS_BUDGET_USD`, default $5) reserves the worst case before each submission, settles it from the job's real start and finish times, and refuses a run that would exceed the cap. It needs `NEBIUS_PRICE_PER_HOUR_USD` to estimate cost.
- **Cancellation.** Cancelling a Kinesis job cancels the Nebius job.
- **Retries.** A submission is retried twice on 429, 5xx or a timeout (FAILURE_MODES #19). A 4xx response is never retried.
- **Failure reasons.** A job that ends `FAILED` or `ERROR` raises `WorkerCrashed` with the platform's reason. A worker-handled failure is reported as `WorkerReportedFailure`, as it is locally.
- **Traces.** Per-state timestamps and the cost estimate go to `work/<node>.nebius.json` and are logged as `nebius.run`.
- **Uploads.** Unchanged inputs (the scene, mostly) are not uploaded again.

### Tests

- **`tests/unit/test_nebius_runner.py` (23 tests), against the fake API in `kinesis.testing.fake_nebius`:**
  - the JWT signature is verified with the public key, and the token is cached and refreshed after a 401;
  - the job body matches the documented contract;
  - the happy path records every state and the cost;
  - a crashed container, a worker-handled failure, and a platform ERROR are each reported correctly;
  - the watchdog cancels a hung job;
  - polling backs off to its cap;
  - cancelling the task cancels the Nebius job;
  - a 503 or 429 on submit is retried, and a 400 is not;
  - the budget guard refuses over-budget runs;
  - a session serves every step in one job, ends early correctly, and handles failures;
  - **a whole Kinesis job on each design produces metrics identical to the local runner.**
- **Fault scenario #19** (`serverless_job_failure_surfaces_error`) is live. A quota `ERROR` fails `extract_scope` with the platform's reason.
- **The session loop was run in the real worker image** against a local job directory. A queued inspect and extract both produced valid results, a request for a spec outside `/work` was refused (exit 2), and `STOP` ended the session.
- **The live test** (`tests/integration/test_nebius_jobs_live.py`, marker `live_nebius_jobs`) runs one inspect step. It is skipped unless `KINESIS_LIVE_NEBIUS_JOBS=1` and the configuration is complete. Run it manually with `uv run task live-nebius-jobs-test`; it **costs money**.

### Two bugs the tests found

1. **Upload-time inspect leaked a session.** It started a session on the scene directory and never closed it, which on Nebius would have left a VM idling for 10 minutes. The service now releases runner resources after inspect, after the repair DAG, and after the export.
2. **The fake API hid a state.** It advanced state before reporting it, so `PROVISIONING` was never observable. It now reports first, as a real first poll would.

## To switch it on (owner)

Once the Nebius account is active again:
1. Create a project in `us-central1`, a container registry, a bucket, and a service account with an authorized key (save the PEM to `C:/Users/<you>/.nebius/kinesis-runner.pem`).
2. Push the image: `uv run task worker-image`, then tag and push it to the registry.
3. Put these in `.env`:
   - `KINESIS_JOB_RUNNER=nebius`
   - `NEBIUS_PROJECT_ID`
   - `NEBIUS_JOB_IMAGE`
   - `NEBIUS_BUCKET`
   - `NEBIUS_S3_ACCESS_KEY_ID` / `NEBIUS_S3_SECRET_ACCESS_KEY`
   - `NEBIUS_SERVICE_ACCOUNT_ID`
   - `NEBIUS_AUTH_KEY_ID`
   - `NEBIUS_AUTH_PEM`
   - `NEBIUS_PRICE_PER_HOUR_USD` for `cpu-d3` / `4vcpu-16gb`
   - and, if needed, `NEBIUS_RUNNER_MODE`, `NEBIUS_PLATFORM`, `NEBIUS_PRESET` and `NEBIUS_PREEMPTIBLE`.

   The runner reports every missing value at once.
4. Run `uv run task live-nebius-jobs-test`, then the golden parity job. Then replace the EXPECTED rows above with measured numbers.

The compute side of C9 (cost per repair) follows from step 4.
