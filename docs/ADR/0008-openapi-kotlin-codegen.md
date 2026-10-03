# ADR 0008: OpenAPI → Kotlin model generation

**Status:** Accepted

## Context
Hand-written copies of the backend DTOs drift away from the backend over time.

## Decision
- **The spec is exported and committed.** `scripts/export_openapi.py` writes `docs/api/openapi.json` from the FastAPI app. The file is committed to the repo.
- **CI checks for drift.** CI re-exports the spec and fails if it differs from the committed copy.
- **Only models are generated.** The Kotlin client uses the openapi-generator Gradle plugin with kotlinx-serialization. It generates models only, into `build/generated`.
- **API calls are hand-written.** They go through a thin Ktor layer, because the generated API clients are heavy.

## Consequences
- A schema change shows up as a Kotlin compile error.
- Generator quirks are controlled by pinning the generator version and config. Serialization round-trip tests against sample JSON cover them.
