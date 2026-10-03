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

## Rules for schema authors

These rules were learned in Phase 0 by running the generator. Each one keeps the generated Kotlin types clean.

| Rule | Reason |
|---|---|
| Fixed-length tuples (`Vec3`) declare a plain `array` + `items` schema via `WithJsonSchema` | `prefixItems` generates `List<Any>`, which kotlinx cannot serialize |
| No `Literal[True]` or integer `Literal[...]` fields. Use `bool` or `int` plus a validator | The generator turns them into string-serialized enums, which mismatch the wire format |
| No `computed_field` on API models. Use stored fields or plain properties | Computed fields split schemas into `-Input` and `-Output` variants |
| Free-form JSON is typed as `JsonValue` | Mapped to `kotlinx.serialization.json.JsonElement` through `typeMappings` and `importMappings` |
| JSON `number` maps to `kotlin.Double` | Avoids `BigDecimal`, which has no default kotlinx serializer. A post-generation step in `build.gradle.kts` rewrites the generator's BigDecimal-style defaults |

`scripts/export_samples.py` builds sample payloads from the real Pydantic models, and the Kotlin tests decode them. CI drift checks keep both the samples and `openapi.json` current.
