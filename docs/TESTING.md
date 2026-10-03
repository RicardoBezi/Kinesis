# Kinesis: Testing Strategy

The testing foundation exists **before** feature code. Every phase turns on its placeholder tests, which are already committed and skipped with a "Phase N" reason. A phase is not done while its placeholders are still skipped.

## Principles

1. **Deterministic first.** Measured metrics carry more weight than AI commentary, and every metric is reproducible in CI.
2. **No network, no credentials** in the default suite. Live Nebius tests require both the `live_nebius` marker and `KINESIS_LIVE_NEBIUS=1`.
3. **No Blender** in the default suite. A numpy mirror of the fixture (`kinesis.testing.synthetic`) feeds the unit and golden tests. Blender tests (`-m blender`) prove that the mirror matches the real fixture.
4. **Thresholds, not exact floats.** Golden tests compare against `tests/golden/thresholds.json`, and measured snapshots are kept for diffing.
5. **An invalid plan is never executed.** Every AI failure mode has a test that ends in a fallback plan or a DEGRADED evaluation.

## Layout and markers

```
tests/
  conftest.py              shared fixtures (selection, scope, fixture_truth), live-test gate
  unit/                    pure logic: schemas, plan gate, DAG definition, interfaces, API contract
  contract/                AI response fixtures through the validation gate; live tests (gated)
  fault/                   fault-injection scenarios (Phase 3)
  golden/                  thresholds.json + golden tests on the numpy mirror; snapshots
  integration/             API behaviour with fakes (Phase 3); Blender worker (-m blender)
  fixtures/ai_responses/   synthetic Token Factory responses (see its README)
```

| Marker | Selected by default | Command |
|---|---|---|
| none | yes | `uv run task test` |
| `contract` | yes | `uv run pytest -m contract` |
| `fault` | yes | `uv run pytest -m fault` |
| `golden` | yes (numpy mirror) | `uv run task test-golden` |
| `blender` | **no** | `uv run task test-blender` |
| `live_nebius` | **no** (double-gated) | `uv run task live-nebius-test` |

The pytest config sets `filterwarnings = error`, so new deprecations fail the build instead of piling up.

## Testing matrix

| Area | Layer | What is asserted | Phase |
|---|---|---|---|
| Schema validation | unit | bounds, `extra=forbid`, frozen, names constrained, inverted ranges rejected, provenance rules, status transitions | **0 ✅** |
| Plan gate | unit + contract | every malformed, unsupported, out-of-scope or out-of-bounds plan becomes FALLBACK; valid plans become MODEL; contact target never taken from the model | **0 ✅** |
| Candidate identity | unit (hypothesis) | the id is deterministic, and changes with input, parameters and algorithm version | **0 ✅** |
| DAG definition | unit | stable topological order, cycle, unknown-dependency and duplicate detection, join semantics (ANY_SUCCESS survives a failed branch), capped backoff, retry classification | **0 ✅** |
| Cache keys | unit | deterministic, independent of key order, namespaced | **0 ✅** |
| Job-dir confinement | unit | `..`, absolute and UNC paths are rejected | **0 ✅** |
| Import purity | unit | the numpy core imports no fastapi, redis, httpx or bpy; no pickle, eval, exec or `shell=True` anywhere | **0 ✅** |
| API contract | unit | every route exists; errors are `application/problem+json`; 422 problems on bad input; the OpenAPI file is current; the required schemas are present | **0 ✅** |
| Temporal / skeletal cropping | unit | context clipping, chain resolution on the fixture rig, `UNSUPPORTED_RIG` | 1 |
| Coordinate transforms / FK | unit + blender | numpy FK equals Blender `pose.bones[].matrix` within 0.1 mm (spike S2) | 1 |
| Trajectory extraction | blender | extract output validates and matches the mirror | 1 |
| Planted-contact detection | unit + golden | the fixture interval is [40, 90] ± 2 and displacement is 9.5–10.5 cm; edge cases (all planted, never planted, gaps, short runs) | 1 |
| Slip metrics | unit | hand-computed micro-trajectories | 1 |
| Two-bone IK | unit (hypothesis) | reaches reachable targets within 1e-6 m, preserves the knee plane relative to the pole, clamps unreachable targets and counts them | 2 |
| Candidate generation | unit + golden | presets are reproducible; A and B meet their thresholds | 2 |
| Preservation invariants | golden (hypothesis) | a left-foot repair leaves RIGHT_HAND unchanged (≤ 1e-6 m); inputs are not mutated; no keys outside `[a−k, b+k]`; idempotent | 2 |
| Objective metrics and ranking | unit | each formula from ALGORITHMS §4; gating | 2 |
| Non-destructive apply | blender | the original Action hash is unchanged; the NLA track exists; muting it restores the original exactly | 2 |
| Render | blender | the expected frame count and resolution | 2 |
| Orchestration | unit + fault | concurrency (A and B overlap in time), retries, breaker, cancellation, partial failure | 3 |
| API behaviour | integration | the happy path through the decision; the invalid cases in spec §11; idempotency | 3 |
| Provider transport | contract | 429/500/400/timeout classification; fence stripping; usage parsed | 4 |
| Live Nebius | live | model discovery, plan, multimodal evaluation | 4 |
| Kotlin client | gradle test | serialization round-trips; state reducer; A/B selection; poll recovery; loading and error states | 0 (serialization) / 5 |
| Serverless runner | integration | the same golden results via NebiusJobRunner (fake in CI) | 6 |

## Golden thresholds

The canonical fixture is `foot_slide_v1`, with 10 cm injected over frames 50–80 ([FIXTURE.md](FIXTURE.md)). The source of truth for thresholds is [`tests/golden/thresholds.json`](../tests/golden/thresholds.json).

| Metric | Threshold | Rationale |
|---|---|---|
| Detected `planted_displacement_cm` | 9.5–10.5 | ±5% of the injected slide, allowing for smoothstep corners and frame sampling |
| Detected interval | [40, 90] ± 2 frames | Lift and land frames sit within the gap and run cleanup tolerance |
| Best candidate `slip_reduction_pct` | ≥ 80 | Spec acceptance target |
| Candidate A `slip_reduction_pct` | ≥ 95 | Full lock; the residual comes only from the tolerance snap and the IK clamp |
| Candidate B `slip_reduction_pct` | ≥ 70 | Expected ≈ 85 by construction ([ALGORITHMS §3.2](ALGORITHMS.md#32-preset-candidates)) |
| `collateral_max_cm` | ≤ 0.01 | Non-chain bones are never keyed; the slack covers float32 storage in `.blend` |
| `outside_window_max_cm` | ≤ 0.001 | No keys outside the window |
| `joint_limit_violations` | 0 | |
| `penetration_max_cm` | ≤ 0.5 | Height clamp |
| `jerk_rms_ratio` | ≤ 1.5 | A short blend (A, k=3) increases jerk; this bounds how much |

**Calibration rule.** Phase 2 measures the real values and writes `tests/golden/foot_slide_v1.json`. A threshold may change only in a commit that updates this table with a reason, and it must never be loosened below the spec's 80% target.

## Fault-injection matrix

The scenarios are listed in `tests/fault/test_fault_injection.py`, and their expected behaviour is in [FAILURE_MODES.md](FAILURE_MODES.md). They use:
- a `FakeJobRunner`, scriptable per invocation to succeed, crash, time out, or write a malformed result;
- `MockProvider` / `httpx.MockTransport` for model errors;
- `fakeredis` with forced connection errors for the Redis outage;
- a fake clock for breaker and backoff timing.

## Kotlin tests

`client/kotlin-desktop/src/test`:
- **Phase 0:** generated-model serialization round-trips against sample JSON copied from the OpenAPI examples.
- **Phase 5:**
  - the job-state reducer, covering every `JobStatus` transition;
  - A/B selection state;
  - the poller, which resumes from `next_seq` after a simulated disconnect (Ktor `MockEngine`);
  - loading and error states, rendered from a Problem body.

## CI mapping

See [DEPLOYMENT.md § CI](DEPLOYMENT.md#ci). In short:
- `ci.yml` runs everything except `blender` and `live_nebius` on every push and PR.
- `blender.yml` runs Blender tests when the paths it depends on change, nightly, and on manual dispatch.
- `live-nebius.yml` runs only on manual dispatch.
