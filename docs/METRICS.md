# Kinesis: Metrics

Kinesis succeeds only if it **improves a workflow**. No productivity claim is made without evidence collected through the protocol below.

## 1. Product metrics

| Metric | Definition | Source |
|---|---|---|
| **Time to acceptable repair** | Wall-clock time from "defect noticed" to "repair accepted". Compared between the manual baseline and Kinesis-assisted repair | Study protocol (§3) plus `time_to_decision_s` and job latency |
| **Manual operations avoided** | Keyframe and curve edits in the manual baseline, minus edits after Kinesis apply (if any) | Study protocol (§3) |
| **Foot-slip reduction** | `slip_reduction_pct` of the **selected** candidate | `CandidateMetrics` |
| **Collateral-motion error** | `collateral_max_cm` and `outside_window_max_cm` of the selected candidate | `CandidateMetrics` |
| **Candidate acceptance rate** | Jobs decided A or B ÷ jobs decided (A, B or REJECT_ALL) | `HumanDecision` |
| **Model/human agreement** | Decisions where `agreed = true` ÷ decisions where the model recommended | `HumanDecision.agreed` |
| **Compute cost per accepted repair** | (Token Factory cost + Serverless compute cost) for all jobs ÷ accepted jobs | provider usage × price table, plus runner duration × rate |

`GET /v1/stats/product` serves these from SQLite as `ProductStats`.

## 2. Operational metrics

**Prometheus**, served at `GET /v1/metrics`:

| Metric | Type | Labels |
|---|---|---|
| `kinesis_job_duration_seconds` | histogram | `outcome` |
| `kinesis_node_duration_seconds` | histogram | `node` |
| `kinesis_worker_duration_seconds` | histogram | `command`, `runner` |
| `kinesis_render_duration_seconds` | histogram | `runner` |
| `kinesis_provider_calls_total` | counter | `model`, `task`, `outcome` (ok/invalid/timeout/429/5xx/4xx/circuit_open) |
| `kinesis_provider_latency_seconds` | histogram | `model`, `task` |
| `kinesis_provider_tokens_total` | counter | `model`, `kind` (prompt/completion) |
| `kinesis_provider_cost_usd_total` | counter | `model` |
| `kinesis_cache_requests_total` | counter | `namespace`, `result` (hit/miss/degraded) |
| `kinesis_retries_total` | counter | `node` |
| `kinesis_candidates_total` | counter | `status` |
| `kinesis_plan_source_total` | counter | `source` (MODEL/FALLBACK) |

**Structured logs** are JSON lines. Every record carries these fields where they apply:
- job and node context: `job_id`, `node`, `candidate_id`, `attempt`, `elapsed_ms`;
- selection: `frame_range`, `target_bones`;
- model usage: `model`, `prompt_tokens`, `completion_tokens`, `cost_estimate_usd`;
- rendering: `render_ms`.

A `contextvars.ContextVar` carries `job_id`, `node`, `candidate_id` and `attempt`, so library code never has to pass them around.

**Cost estimates** come from a price table in configuration, in USD per 1M tokens per model and USD per second per runner. The table must be checked against current Nebius pricing before any number is published. If no price is configured, the estimate is `null`, never a guess.

## 3. Workflow evaluation protocol (to be run in Phase 7)

The productivity claim rests on this protocol. Until it has been run, the README and portfolio materials claim only measured *objective* improvements, such as "slip reduced from 10.0 cm to 0.3 cm".

1. **Scenes.** Use the canonical fixture plus 2–3 variants: a different slide magnitude and direction, a right foot, and a longer contact. Use 1 externally sourced, CC-licensed walk cycle with a hand-introduced slide.
2. **Participants.** At least 3 people with Blender animation experience. Report each participant's experience level.
3. **Manual condition.** Fix the slide using any standard Blender technique. Record screen and time. Count graph editor and keyframe operations, either from the screen recording or from an operator log (an add-on with a `bpy.app.handlers` logger).
4. **Assisted condition.** Use Kinesis end to end, from selection through decision. Measure the same quantities.
5. **Counterbalancing.** Alternate the order of conditions and the scene assignment.
6. **Acceptance.** A repair counts as acceptable when the participant says so **and** the objective slip is below 1 cm.
7. **Report.** Give the median and range per condition, and per-participant raw data, in `docs/evaluation/`. Note honestly where the manual approach won.

## 4. MVP definition-of-done evidence

Step 13 of the definition of done is satisfied by one reproducible run that records these items in the job's events and logs:
- total latency, and latency per node;
- model ids and token usage;
- the slip before and after for A and B;
- `collateral_max_cm`.

A Phase 7 script exports that run as `docs/evaluation/demo_run.json`.
