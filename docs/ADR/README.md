# Architecture Decision Records

Each ADR follows the format Context → Decision → Consequences. Status is one of Proposed, Accepted or Superseded. When an ADR depends on an assumption that has not been verified yet, it names the spike that will verify it.

| # | Title | Status |
|---|---|---|
| [0001](0001-monorepo-layout.md) | Monorepo and layout | Accepted |
| [0002](0002-ai-plans-deterministic-executes.md) | AI plans and evaluates; deterministic code executes | Accepted |
| [0003](0003-numpy-core-thin-blender-worker.md) | Pure-numpy core, thin Blender worker | Accepted (S2 passed) |
| [0004](0004-non-destructive-nla-layering.md) | Non-destructive repair via a new Action on an NLA track | Accepted (revised by S1) |
| [0005](0005-sqlite-source-of-truth-redis-cache.md) | SQLite is the source of truth; Redis is an optional cache | Accepted |
| [0006](0006-polling-event-log.md) | Progress via a polled event log | Accepted |
| [0007](0007-frame-sequence-previews.md) | Previews as frame sequences | Accepted (S3 passed locally and on CI) |
| [0008](0008-openapi-kotlin-codegen.md) | OpenAPI → Kotlin model generation | Accepted |
| [0009](0009-httpx-token-factory-client.md) | httpx client for Token Factory | Accepted (pending S4) |
| [0010](0010-blender-4-5-lts.md) | Pin Blender 4.5 LTS | Accepted |
| [0011](0011-python-task-runner.md) | Python task runner instead of make | Accepted |
