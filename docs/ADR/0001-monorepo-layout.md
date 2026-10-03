# ADR 0001: Monorepo and layout

**Status:** Accepted

## Context
Kinesis spans a Python backend, a Blender worker and add-on, a Kotlin desktop client, infrastructure, and docs. The API contract is shared across all of them. When the API changes, the client has to change in the same commit.

## Decision
- **One repository.** It has top-level `backend/`, `blender/`, `client/kotlin-desktop/`, `infra/`, `spikes/`, `tests/`, `scripts/` and `docs/`.
- **uv workspace for Python.** The root `pyproject.toml` holds the tool config, and `backend/` is a workspace member.
- **Tests by layer.** Python tests live in the top-level `tests/` tree, split into unit, contract, fault, golden and integration.

## Consequences
- **Atomic contract changes.** One PR can change a schema, regenerate `docs/api/openapi.json` and update the Kotlin models. The CI drift check enforces this.
- **Fast CI.** Jobs use path filters so they only run when relevant files change.
