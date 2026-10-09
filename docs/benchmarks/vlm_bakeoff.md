## Vision-model bake-off

Run 2026-10-09 02:37 on commit `225af4b`: 62 calls, $0.1704 total (cap $0.17; an earlier run lost its results after spending $0.13), 3 repeats per condition. Frames: real previews of the canonical fixture.

Conditions with a known answer: Original vs A and Original vs B (the candidate should be preferred), and *swapped* (the repaired A shown as the original, the sliding original as the candidate: it should **not** be preferred).

| Model | Frames | Trials | Accuracy | Ranking agreement (A >= B) | Score stdev | Valid JSON | Median latency | $/review |
|---|---|---|---|---|---|---|---|---|
| `openbmb/MiniCPM-V-4_5` | plain | 9 | 0.67 | 1.00 | 0.16 | 1.00 | 1875 ms | 0.00166 |
| `openbmb/MiniCPM-V-4_5` | annotated | 7 | 0.71 | 1.00 | 0.00 | 1.00 | 1937 ms | 0.00166 |
| `Qwen/Qwen3.8-27B` | plain | 9 | 1.00 | 1.00 | 0.24 | 1.00 | 4875 ms | 0.00405 |
| `Qwen/Qwen3.8-27B` | annotated | 7 | 1.00 | 1.00 | 0.29 | 1.00 | 8063 ms | 0.00624 |
| `deepseek-ai/DeepSeek-V4.1-Flash` | plain | 9 | 1.00 | 1.00 | 0.12 | 0.89 | 6687 ms | 0.00261 |
| `deepseek-ai/DeepSeek-V4.1-Flash` | annotated | 6 | 1.00 | 1.00 | 0.12 | 0.83 | 13710 ms | 0.00423 |
| `zai-org/GLM-5.3-Flash` | plain | 9 | 1.00 | 1.00 | 0.12 | 1.00 | 9703 ms | 0.00099 |
| `zai-org/GLM-5.3-Flash` | annotated | 6 | 1.00 | 1.00 | 0.12 | 1.00 | 9499 ms | 0.00098 |

**Winner:** `zai-org/GLM-5.3-Flash` (plain frames).

Wrong answers by condition (valid responses only):

- `openbmb/MiniCPM-V-4_5`: orig_vs_B x5

Accuracy is the share of valid answers whose `prefers_over_original` matches the known truth. The samples are small (6-9 trials per row; the run stopped at its cost cap), so read 1.00 as "no miss observed", not as a guarantee.
