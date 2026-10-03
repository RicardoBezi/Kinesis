# Kinesis: Local Development, CI and Deployment

## Local development

### Prerequisites
| Tool | Required? | Notes |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | **yes** | Manages Python 3.12 and all dependencies |
| Git | **yes** | |
| Blender 4.5 LTS | for Blender tests and real jobs | `uv run task setup-blender` downloads the pinned portable build (4.5.14, checksum-verified) into `.tools/`. It sits alongside any other Blender install |
| JDK 17+ | for the Kotlin client | The Gradle toolchain auto-provisions it if you only have an older JDK |
| Docker Desktop | optional | For `docker compose up` |

### Commands
```bash
uv run task setup            # uv sync + .env from .env.example + tool report
uv run task test             # default offline suite (no Blender, no network)
uv run task lint             # ruff format --check, ruff check, mypy --strict
uv run task fmt              # auto-format and auto-fix
uv run task run              # API with reload at http://127.0.0.1:8000/docs
uv run task openapi          # regenerate docs/api/openapi.json (CI checks drift)
uv run task setup-blender    # portable Blender 4.5.14 into .tools/
uv run task fixture          # (Phase 1) build blender/fixtures/foot_slide_v1.blend
uv run task test-blender     # Blender integration + golden tests
uv run task models           # (spike S4) list Token Factory models for your key
uv run task live-nebius-test # live Token Factory tests; needs NEBIUS_API_KEY
uv run task client           # Compose Desktop review client
```

### Containers
`docker compose up` starts:
- **`api`:** FastAPI on port 8000, with `./var` mounted for the database and artifacts. `KINESIS_REDIS_URL=redis://redis:6379/0`.
- **`redis`:** `redis:7-alpine`, no persistence (it is a cache, see ADR 0005).

Blender is **not** in the default compose stack. GPU and display access in containers varies too much from machine to machine. Instead:
- **Local runs:** the API calls a host Blender through `KINESIS_BLENDER_BIN`.
- **Phase 6:** a `worker` compose profile adds `infra/docker/blender-worker.Dockerfile` (Blender 4.5 on Debian, Workbench via EGL/llvmpipe). The same image is what runs on Nebius Serverless Jobs.

### Development without anything optional
- **No Redis:** set `KINESIS_REDIS_URL` empty and an in-process cache is used.
- **No AI:** set `KINESIS_PROVIDER=null` and the deterministic plan is used, with evaluation SKIPPED.
- **No Blender:** unit, golden (numpy mirror), contract and API tests all run anyway.

### OneDrive / synced folders
If the repo lives in a synced folder such as OneDrive or Dropbox, exclude these from sync. They churn heavily and can hit file locks:
- `.venv/`, `.tools/`, `var/`
- `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`
- `client/kotlin-desktop/build/`, `client/kotlin-desktop/.gradle/`

Better still, clone the repo outside the synced folder.

## CI

All workflows live in [`.github/workflows/`](../.github/workflows/). **No workflow that gates a merge calls Nebius.**

| Workflow | Trigger | Jobs | Required to merge |
|---|---|---|---|
| `ci.yml` | push, pull_request | `python` (format, lint, mypy strict, pytest with a Redis service), `openapi-drift`, `kotlin` (build + test, JDK 17), `docker` (build backend image) | **yes**, all four |
| `blender.yml` | changes under `blender/**`, `backend/src/kinesis/{analysis,repair,evaluation,schemas}/**`, `tests/integration/**`, `tests/golden/**`; nightly 03:00 UTC; manual | downloads (cached) Blender 4.5.14, runs `pytest -m blender` | not yet. It becomes required once spike S3 shows headless rendering is reliable on hosted runners |
| `live-nebius.yml` | `workflow_dispatch` only | `pytest -m live_nebius` with the `NEBIUS_API_KEY` secret | never |

**Why Blender is not in `ci.yml`.** The Blender tarball is about 350 MB. It is cached, but cold runs still add minutes. Headless rendering on GPU-less runners has to be proven first (spike S3). Every Blender-independent guarantee is still enforced on every push through the numpy mirror.

## Secrets

| Secret | Where | Used by |
|---|---|---|
| `NEBIUS_API_KEY` | local `.env` (git-ignored); GitHub Actions secret | Token Factory (Phase 4), `live-nebius.yml` |
| `NEBIUS_PROJECT_ID`, `NEBIUS_JOB_IMAGE` | `.env` / deployment env | Serverless Jobs (Phase 6) |

Secrets are read only from the environment through `pydantic-settings`. `SecretStr` keeps them out of logs and reprs.

## Nebius deployment (Phase 6, outline)

See [`infra/nebius/README.md`](../infra/nebius/README.md). In outline:
- **API:** a single container (`infra/docker/backend.Dockerfile`) on a Nebius VM or container service, with a persistent volume for SQLite and artifacts. Redis is managed or co-located.
- **Workers:** `NebiusJobRunner` submits `blender-worker` image runs as Serverless Jobs.
  - Each run gets exactly one job directory, exchanged through Object Storage: an input prefix for the spec and scene, an output prefix for the result and frames.
  - Workers get no other filesystem access.
- **Models:** Token Factory over HTTPS, with the model ids from configuration (spike S4).
- **Measured, not assumed:** local vs. serverless latency and cost, reported in METRICS.md.

Spike S5 must still verify the Serverless Jobs submission API, artifact transfer, and status delivery (push vs. poll) before Phase 6 starts.
