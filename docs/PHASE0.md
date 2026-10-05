# Phase 0: design freeze, with an index of deliverables

Phase 0 asked for 15 deliverables. Each one is listed here with where it lives.

| # | Deliverable | Where |
|---|---|---|
| 1 | Final repository tree | [ARCHITECTURE §9](ARCHITECTURE.md#9-repository-layout), and the repository itself |
| 2 | Architecture diagram (Mermaid) | [ARCHITECTURE §1–3](ARCHITECTURE.md) (component, sequence and DAG views) |
| 3 | Concrete Pydantic schemas | [`backend/src/kinesis/schemas/`](../backend/src/kinesis/schemas/), tested in `tests/unit/test_schemas.py` |
| 4 | FastAPI endpoint contracts | [API.md](API.md), [`api/openapi.json`](api/openapi.json), stubs in `backend/src/kinesis/api/` |
| 5 | DAG node definitions and dependencies | [ARCHITECTURE §3](ARCHITECTURE.md#3-dag-definition), `kinesis.orchestration.dag` (`validate_graph`, `should_run`) |
| 6 | Blender fixture design | [FIXTURE.md](FIXTURE.md), [`fixture_truth.json`](../blender/fixtures/fixture_truth.json) |
| 7 | Exact foot-slide detection algorithm | [ALGORITHMS §2](ALGORITHMS.md#2-foot-slide-detection) |
| 8 | Exact Candidate A/B repair strategies | [ALGORITHMS §3](ALGORITHMS.md#3-candidate-repair), `DEFAULT_CANDIDATES` in `schemas/plan.py` |
| 9 | Testing matrix | [TESTING.md](TESTING.md#testing-matrix) |
| 10 | Failure-mode matrix | [FAILURE_MODES.md](FAILURE_MODES.md) |
| 11 | Local development setup | [DEPLOYMENT.md § Local development](DEPLOYMENT.md#local-development), `scripts/tasks.py`, `docker-compose.yml` |
| 12 | CI plan | [DEPLOYMENT.md § CI](DEPLOYMENT.md#ci), `.github/workflows/` |
| 13 | Dependency list with reasons | [ARCHITECTURE §10](ARCHITECTURE.md#10-dependencies) |
| 14 | Phase 0 files | All of the above, plus [ADRs](ADR/), [spikes](../spikes/README.md), the test harness and the Kotlin skeleton |
| 15 | Architectural concerns and assumptions | Below |

## What Phase 0 already proved, as opposed to only designing

- **The plan gate works.** It turns every malformed, unsupported, out-of-scope, out-of-bounds or provenance-forging model plan into the deterministic fallback. This is shown by 19 synthetic responses plus parametrized unit tests.
- **The OpenAPI contract round-trips into Kotlin.** Backend-generated sample payloads decode with the generated Kotlin models. Doing this exposed and fixed three schema shapes that would have broken the client.
- **S1 disproved the first NLA layout before any code depended on it.** It showed that the original must be moved to a bottom NLA strip, not left as the active action. ADR 0004 has been revised.
- **S2 confirmed numpy FK parity** with Blender to 0.6 µm.
- **S3 confirmed headless Workbench rendering:** 0.057 s/frame on the workstation and 0.417 s/frame on GPU-less GitHub CI. S1 and S2 reproduce on Linux CI.

## Open concerns and assumptions (resolve before or during the phase noted)

| # | Concern | Resolve by | Owner action |
|---|---|---|---|
| C1 | ~~Token Factory specifics~~ **Resolved by S4:** all confirmed, except that **no multimodal Nemotron exists**. Vision evaluation uses MiniCPM-V-4.5 (≤ 10 images per request) | Done | Decide whether a non-NVIDIA vision model is acceptable for the portfolio narrative |
| C2 | **The Serverless Jobs API and its artifact exchange are unverified** (spike S5) | Before Phase 6 | Read the Nebius docs or do a trial run, then answer the questions in `infra/nebius/README.md` |
| C3 | ~~S3 on GPU-less CI~~ **Resolved:** 0.417 s/frame on ubuntu-latest. Make `blender.yml` a required gate after a week of green nightly runs | Phase 1 | Enable branch protection for `blender` |
| C4 | **Live add-on workflow:** the MVP works on an uploaded copy and produces an output `.blend`. Importing the chosen Action back into the animator's open session is an add-on feature, and it must use the same pushed-original NLA layout | Phase 5/7 | n/a |
| C5 | **The productivity claim needs a user protocol** with at least 3 animators, as described in [METRICS §3](METRICS.md#3-workflow-evaluation-protocol-to-be-run-in-phase-7). Until it has been run, only objective slip and collateral numbers can be claimed | Phase 7 | Recruit participants |
| C6 | **The MVP chain resolver assumes a thigh → shin → foot hierarchy.** Production rigs with IK controls, twist bones or FK/IK switches will hit `UNSUPPORTED_RIG` | Post-MVP | A rig-mapping config is a future milestone |
| C7 | **Scaled armature objects** are not covered by S2 | Phase 1 | Add a scaled case to S2, or reject scaled rigs in `inspect` |
| C8 | ~~The repo lives in OneDrive~~ **Resolved 2026-10-05:** the repo moved to `C:\dev\Kinesis`, outside any synced folder | Done | n/a |
| C9 | **Price tables for the cost-per-repair metric** must come from current Nebius pricing. Until they do, the estimate is reported as `null` | Phase 4/6 | n/a |
| C10 | **Generator quirk:** openapi-generator 7.14 emits BigDecimal-style defaults, which are patched in `build.gradle.kts`. Re-check this when bumping the generator | On upgrade | n/a |

## Next: Phase 1 (fixture and analysis)

1. Implement `kinesis.testing.synthetic.foot_slide_v1()`, the numpy mirror, using the formulas in FIXTURE.md.
2. Implement `blender/fixtures/build_fixture.py` from the same formulas, and commit `foot_slide_v1.blend`.
3. Implement the worker's `inspect` and `extract` commands, `LocalJobRunner`, and scope resolution.
4. Implement detection as specified in ALGORITHMS §2. Turn on the Phase 1 golden and Blender tests.
5. **Exit criterion:** Kinesis reports about 10 cm of planted displacement on frames 40–90 of the fixture, in CI (numpy mirror) and in `blender.yml` (real fixture).
