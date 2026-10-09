# ADR 0009: httpx client for Nebius Token Factory

**Status:** Accepted. Spike S4 passed on 2026-10-05, with one deviation (see below)

## Context
Token Factory exposes an OpenAI-compatible API. Kinesis needs three calls from it:
- chat completions with text;
- chat completions with images;
- listing the available models.

## Decision
Build `TokenFactoryClient` on `httpx.AsyncClient`:
- **Explicit shapes:** the request and response formats are spelled out in code.
- **Timeout:** every request has one.
- **Error classification:** timeouts, 429 and 5xx are retryable. Other 4xx errors are fatal.
- **Usage tracking:** the client parses the `usage` block and records the token counts.
- **Testing:** tests inject `httpx.MockTransport` instead of calling the real service.

## Assumptions to verify (S4)
- the base URL and the authentication header;
- that `/v1/models` lists the available models;
- the exact Nemotron 3 Super and Nano Omni model IDs;
- support for `response_format: {type: json_schema}`. If it is missing, fall back to JSON mode and validate afterwards;
- the image input format (`image_url` as a data URL) and the maximum number of images per request;
- that responses include `usage`.

## Consequences
- **No vendor SDK dependency.** Kinesis does not depend on the release schedule of a vendor SDK.
- **Slightly more code to maintain.** That is acceptable for three endpoints.

## S4 result and deviation (2026-10-05)
- **Confirmed:** the base URL `https://api.tokenfactory.nebius.com/v1/` with Bearer auth; `json_schema` structured output on Nemotron Super, Nemotron Nano and MiniCPM-V; data-URL image input; `usage` including reasoning tokens.
- **Deviation:** Token Factory has **no image-capable Nemotron**, so the "Nemotron 3 Nano Omni" in the spec is unavailable. Visual evaluation uses `openbmb/MiniCPM-V-4_5` behind the same `ModelProvider` interface, with at most 10 images per request. Planning and classification stay on Nemotron (Super and Nano). If NVIDIA releases a multimodal Nemotron on Token Factory, switching to it is a one-line change to `KINESIS_VISION_MODEL` plus a contract re-check.
- **Startup check:** `GET /models?verbose=true` exposes modality and features. At startup the provider verifies that the configured models exist and that the vision model is `text+image->text`, and fails fast if not.

## Phase 4 live findings (2026-10-08)
- **Pricing units.** The catalog's `pricing.prompt` and `pricing.completion` are **USD per token** (for example, `"0.0000003"` = $0.30 per 1M). The client converts them to per-1M on load.
- **Feature tags are incomplete.** Nemotron Super is tagged only `tools` and `reasoning`, but honours strict `json_schema`. A missing `structured_outputs` tag therefore only logs a warning; it is not a startup failure.
- **Measured calls:**
  - planner: about 0.8k prompt and 0.8–1.2k completion tokens, 4–6 s, about $0.001;
  - vision with 10 frames: about 1.1k prompt tokens, under 1 s, about $0.0008.

## Vision-model bake-off (2026-10-09)

The owner asked for a bake-off before settling the vision model, because no NVIDIA vision-language model is available on Token Factory. The full results are in `docs/benchmarks/vlm_bakeoff.md`, produced by `uv run task vlm-bakeoff`.

**Design**
- Four candidates were tested on real fixture previews, with the same camera for every pane.
- Every condition has a known answer:
  - Original vs A: the candidate should be preferred;
  - Original vs B: the candidate should be preferred;
  - *swapped*, with A presented as the original: the candidate must not be preferred.
- Frames were shown in two styles, plain and annotated.

**Cost:** $0.17 for 62 calls. An earlier run of about $0.13 lost its results to a cleanup crash that has since been fixed.

**Results**

| Model | Wrong answers | Valid JSON | Cost per review | Latency |
|---|---|---|---|---|
| `zai-org/GLM-5.3-Flash` | none | 100% | about $0.001 (cheapest) | about 9.7 s |
| `Qwen/Qwen3.8-27B` | none | 100% | about $0.004–0.006 | |
| `deepseek-ai/DeepSeek-V4.1-Flash` | none | 83–89%: empty responses | | |
| `openbmb/MiniCPM-V-4_5` (previous default) | 5, all on Original vs B | | | about 1.9 s (fastest) |

MiniCPM consistently judged that B (a partial fix) was not better than the original.

**Decision:** the vision default is now `zai-org/GLM-5.3-Flash`.
- The evaluator's output budget is raised to 4096 tokens, because GLM reasons before it answers.
- The two candidate reviews run concurrently, so a job waits about 10 s once rather than twice.
- Plain frames are used; annotation made no difference for the winner.
- The samples are small: 6–9 trials per row. A 1.00 score means "no miss observed", not a guarantee.
