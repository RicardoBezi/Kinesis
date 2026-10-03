# Synthetic AI responses

These are hand-built responses in the OpenAI-compatible chat-completions shape. Normal CI uses them so that the AI contract tests never touch the network.

Each file has these fields:

| Field | Meaning |
|---|---|
| `task` | `plan` or `evaluate` |
| `kind` | `http` (replayed through `httpx.MockTransport`) or `timeout` (the transport raises `httpx.ReadTimeout`) |
| `status`, `headers`, `body` | The HTTP response to replay |
| `expect` | The outcome the pipeline must produce |

The `expect` values:

| Value | Meaning |
|---|---|
| `accepted` | The plan or evaluation passes validation |
| `accepted_after_fence_strip` | Accepted once the provider strips markdown fences |
| `fallback` | The plan is rejected, so the deterministic fallback plan is used and the invalid plan is never executed |
| `degraded` | The evaluation is rejected, so `evaluator_status=DEGRADED` and the report is objective-only |
| `retryable_error` | Retried by policy, and counted by the circuit breaker |
| `fatal_error` | Not retried, and not counted as a breaker failure |

Model IDs in these files are placeholders. The real IDs come from configuration, verified by spike S4. Recorded live responses can be added here as `recorded_*.json`, with secrets and request IDs scrubbed.
