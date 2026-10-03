# Nebius deployment (Phase 6)

This document is a placeholder that sets out the boundaries before any cloud code is written. All of it depends on **spike S5** (see [spikes/README.md](../../spikes/README.md)).

## Components

| Component | Nebius service | Notes |
|---|---|---|
| Kinesis API | Compute VM or container service | Single instance for the MVP; persistent disk for SQLite and artifacts |
| Redis | Managed Redis, or co-located | Cache only; it can be flushed safely |
| Blender worker | **Serverless Jobs** | `infra/docker/blender-worker.Dockerfile` (Phase 6). Runs one worker command per job |
| Models | **Token Factory** | OpenAI-compatible API; model ids come from configuration |
| Artifact exchange | Object Storage | Input prefix: `spec.json` + `scene.blend`. Output prefix: `result.json` + frames |

## Worker isolation

- The worker has no access to the API's database or to other jobs' prefixes. Each job gets credentials scoped to its own prefix, if Nebius supports that (verify in S5). If it doesn't, use pre-signed URLs.
- The container filesystem is read-only, apart from `/work`, which holds one job directory.
- The argv is fixed, the same as in `LocalJobRunner`, and the command comes from the `WorkerCommand` enum.

## Open questions for S5

1. What is the submission API (REST or SDK)? What are its authentication model and quota limits?
2. What startup latency should we expect for a ~1 GB Blender image? Can the image be cached?
3. Is completion signalled by polling only, or is a webhook or event available? If webhooks exist, events must be idempotent; see FAILURE_MODES #20.
4. What are the CPU-only versus GPU instance shapes and their prices? Workbench rendering may not need a GPU.
5. Which Object Storage client should be used? S3-compatible boto3 would add a dependency, and that choice needs justification.
