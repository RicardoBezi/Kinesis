# Phase 4: Nebius / NVIDIA

**Status: complete (2026-10-08).** With `KINESIS_PROVIDER=token_factory`, two models now take part in each repair:
- **Planner:** Nemotron 3 Super chooses the parameters for candidates A and B, through strict structured output.
- **Visual evaluator:** MiniCPM-V-4.5 reviews the rendered frames.

Every model output still goes through the validation gate, so a bad or unavailable model degrades to the deterministic presets and the objective ranking. **The contract tests pass offline, and the live test passes when the flag is set.**

## Live results (2026-10-08, trial account)

| Call | Model | Tokens (prompt / completion) | Latency | Cost |
|---|---|---|---|---|
| Plan for the fixture defect | `nvidia/nemotron-3-super-120b-a12b` | 774 / 769–1164 (mostly reasoning) | 4.3–6.1 s | $0.0009–0.0013 |
| Visual judgement, 10 frames | `openbmb/MiniCPM-V-4_5` | 1090 / 74 | 0.7–0.8 s | $0.0008 |

- **Plan quality:** every live plan was valid on the first try, and every explanation matched the intended A/B split. One example: "A applies a strong ONSET lock to remove most slip, while B uses a weaker MEAN lock with longer blend to preserve more of the original motion."
- **Total spend:** all of Phase 4's live calls together, including one rerun, cost about $0.006.

## Deliverables

| Deliverable | Where |
|---|---|
| `TokenFactoryClient`: explicit shapes, error classification, `Retry-After`, fence stripping, usage including reasoning tokens | [`providers/token_factory/client.py`](../backend/src/kinesis/providers/token_factory/client.py) |
| Prompts `planner/1` and `evaluator/1`, with hand-written strict JSON schemas | [`providers/token_factory/prompts.py`](../backend/src/kinesis/providers/token_factory/prompts.py) |
| `TokenFactoryProvider`: plan, evaluate, analyze, catalog check, price table | [`providers/token_factory/provider.py`](../backend/src/kinesis/providers/token_factory/provider.py) |
| Recommendation that blends the objective and visual views | [`evaluation/recommend.py`](../backend/src/kinesis/evaluation/recommend.py) |
| `/v1/health/providers` (model provider, Redis, Blender) | [`api/routes_health.py`](../backend/src/kinesis/api/routes_health.py) |
| The full METRICS.md §2 Prometheus set, including provider tokens and cost | [`observability/metrics.py`](../backend/src/kinesis/observability/metrics.py) |
| Tests: transport replay of every synthetic response, provider unit tests, a whole job against a mocked Token Factory, live tests | `tests/contract/`, `tests/unit/test_token_factory.py`, `tests/unit/test_recommend.py`, `tests/integration/test_token_factory_pipeline.py` |

## Findings and design decisions

1. **Catalog prices are USD per token**, not per million: `"0.0000003"` means $0.30 per 1M. The first live run showed this, because every cost came out as zero. Prices are converted on load, and the unit tests use the real format. **This resolves concern C9**: the cost per repair is now estimated from live catalog prices, `kinesis_provider_cost_usd_total`.
2. **Nemotron Super is tagged only `tools` and `reasoning`,** yet it honours strict `json_schema`. This was shown by S4 and again by every live plan. So the startup check fails fast only when the configuration is definitely wrong:
   - a configured model is missing from the catalog;
   - the vision model does not accept images.

   A missing `structured_outputs` tag only logs a warning. If the catalog cannot be reached at startup, Kinesis logs a warning and starts anyway, and the breakers handle the outage.
3. **The JSON schemas are written by hand.** Strict mode wants flat schemas, with no `$ref` and `additionalProperties: false`. A unit test keeps them in step with `ModelPlanProposal`, `CandidateParameters` and `ModelVisualJudgement`: the same fields, the same bounds, and enums that stay inside those bounds.
4. **The animator's instruction is untrusted.** It is quoted inside `<animator_instruction>`, and its angle brackets become parentheses, so it cannot close the block. The system prompts say to treat it as a style preference only. The plan gate still re-validates every field.
5. **Vision requests carry 5 original and 5 candidate frames**, sampled at the same frame numbers and labelled in order. That is the S4 limit of 10 images.
6. **The recommendation blends both views.** When every ungated candidate has a visual judgement, candidates are ranked by `0.7 × objective + 0.3 × visual`, where the visual score is the mean of the five scores mapped onto [0, 1]. Otherwise the objective ranking decides alone. The weights are recorded in `scoring_weights`, and the reason says when the measurements and the visual review disagree. Gated candidates are never recommended.
7. **Metric names follow METRICS.md exactly.** Phase 3 had used provisional names, and they were renamed.
8. **The classifier model is not used yet.** It is configured and checked against the catalog, but nothing calls it. It is reserved for instruction-intent classification once instructions do more than steer the planner.

## Open owner decision

- **C1:** no image-capable Nemotron exists on Token Factory, so vision runs on MiniCPM-V-4.5, which is not an NVIDIA model. Switching to a multimodal Nemotron later is a one-line change to `KINESIS_VISION_MODEL`. The startup check verifies image support.

## Running it

```bash
# .env: KINESIS_PROVIDER=token_factory, NEBIUS_API_KEY=..., model ids as in .env.example
uv run task run                    # startup verifies the catalog, then serves the API
curl localhost:8000/v1/health/providers
uv run task live-nebius-test       # about $0.002 per run; reads the key from .env
```
