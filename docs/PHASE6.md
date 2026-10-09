# Phase 6: serverless

**Status: partly done (2026-10-09); blocked on an owner decision.**

**Done:** the containerized worker, and a `ContainerJobRunner` that runs it, are complete. The container gives **the same golden results as host Blender**, which a test checks on every nightly CI run.

**Blocked:** `NebiusJobRunner`, the piece that would submit the same container to Nebius Serverless Jobs, needs a Nebius project, a decision to spend on compute, and the answers to spike S5. These are concern C2, and they can only come from the project owner.

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

On cancel or timeout the runner force-removes the container. Killing only the `docker` CLI would leave Blender running. This is the same process boundary a serverless job has, so the image is what Nebius would run.

### Local versus container (workstation, 2026-10-09)

| | Upload → review | Notes |
|---|---|---|
| Host Blender (`LocalJobRunner`) | 8.1 s | Renders on the GPU (Workbench, about 0.06 s/frame) |
| Container (`ContainerJobRunner`, 4 CPUs) | 31.0 s | Renders on the CPU through Mesa llvmpipe; the GitHub CPU runner measured about 0.42 s/frame in S3 |
| Container start-up overhead | about 0.45 s per worker command | `docker run` + `blender --version`: 0.55–0.62 s, against 0.12–0.14 s on the host |

A job runs 6 worker commands, so start-up adds about 3 s. The rest of the gap comes from rendering about 250 frames (the A, B and original crops plus context frames) on the CPU. **What this means for Nebius:**
- a GPU instance type would recover most of that time;
- on CPU-only shapes, rendering fewer preview frames (for example every 2nd frame) is the main lever.

Either way, the results are identical.

## Still to do: `NebiusJobRunner` (needs the owner)

The open questions are in [`infra/nebius/README.md`](../infra/nebius/README.md) (spike S5):
1. the submission API and its authentication;
2. image start-up latency for a 2 GB image, and whether the image is cached;
3. how completion is signalled (polling or webhook);
4. CPU and GPU instance shapes and their prices;
5. which Object Storage client to use for exchanging artifacts.

Answering them needs a Nebius project (`NEBIUS_PROJECT_ID`) and a decision to spend on Serverless compute. Once S5 is answered, the runner reuses these existing pieces:
- the same image, pushed to a registry;
- the same argv: `--command <enum> --spec /work/...`;
- a job-directory upload and download through Object Storage;
- the `JobRunner` contract: raise `WorkerTimeout` or `WorkerCrashed`, then return the result path.

The fault scenario `serverless_job_failure_surfaces_error` stays skipped until then.

Two more things wait for that point:
- the exit criterion "the same golden results on both runners", measured against Nebius;
- the compute side of the cost-per-repair metric (C9).
