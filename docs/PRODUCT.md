# Kinesis: Product Definition

## Thesis

Kinesis reduces the time animators spend repairing **localized** animation defects.

Most shots that need cleanup are mostly correct. One body region goes wrong over one frame range: a foot slides, a hand misses its contact, a joint jitters, a limb clips, or an arc is wrong. Today the animator fixes it by repeating a loop:

1. edit curves
2. replay
3. undo
4. try again
5. compare alternatives in their head

Kinesis shortens that loop.

1. **The animator selects:**
   - a part of the rig;
   - a frame range;
   - optionally a contact object;
   - optionally a written instruction.
2. **Kinesis does the rest:**
   1. extracts only the relevant motion context, with temporal and skeletal cropping;
   2. measures the defect deterministically;
   3. proposes a structured repair plan, where AI chooses bounded parameters;
   4. generates several non-destructive repair candidates in parallel;
   5. renders cropped previews;
   6. scores the candidates with deterministic metrics and NVIDIA Nemotron multimodal evaluation;
   7. presents an Original / A / B comparison.
3. **The animator decides:**
   - they choose a candidate, or reject all of them;
   - Kinesis applies the choice as a separate, reversible layer.

**The human is always the final decision maker.** Kinesis improves an existing workflow. It does not replace animators.

## Target user

A character animator or animation cleanup artist working in Blender on a desktop workstation. They are fluent with the graph editor and NLA, and they are impatient with tools that hide what they changed.

## MVP scope: foot-contact / foot-sliding repair

The MVP supports exactly **one** repair class, and supports it well.

> **Example.** The animator selects the character rig, the left leg chain, frames 83–112, and the floor. Kinesis reports: "left foot planted 85–110, but it translates 8.1 cm while planted." It generates **Candidate A** (strong lock) and **Candidate B** (soft correction that keeps more of the original motion). The review client shows synchronized Original / A / B playback with slip distance, smoothness, collateral-motion score and the model's assessment. The animator picks B, and Kinesis writes it as a new NLA layer. The original action is never touched.

### What the animator sees

- synchronized cropped previews (Original / A / B), sharing one frame index
- foot-slip distance before and after, in cm
- motion smoothness (a jerk ratio)
- the collateral-motion score: how much of everything else changed
- the model's structured evaluation and recommendation, clearly labeled as advisory
- the repair parameters and the plan explanation

### Explicit non-goals for the MVP

- generic full-body animation generation
- full motion-capture replacement
- arbitrary scene generation
- Maya support
- AI-generated Blender scripts of **any** kind
- 2D-to-3D reconstruction
- more than one defect category
- automatic acceptance without human review

## Core principle

**AI plans and evaluates. Deterministic software executes.**

```
Nemotron → strict Pydantic RepairPlan (bounded parameters only)
         → semantic validation against the selection
         → whitelisted deterministic repair (numpy IK)
         → Blender writes keyframes into a new Action
```

- An LLM never emits code, and nothing an LLM returns is ever executed.
- A plan that fails validation is replaced by the deterministic default plan.
- The whole pipeline runs without any AI provider.

## Definition of MVP done

The MVP is complete **only** when this exact scenario works reproducibly:

1. Open the canonical Blender fixture (`foot_slide_v1.blend`).
2. Select the left leg and frames that contain the artificial slide (frames 50–80).
3. Kinesis measures the original defect: about 10 cm of planted displacement.
4. Submit the repair.
5. Two repair candidates run **concurrently**.
6. The original source animation stays byte-for-byte unchanged.
7. Both candidates produce previews and deterministic metrics.
8. At least one candidate reduces planted displacement by **≥ 80%** (the golden threshold).
9. Nemotron evaluates the candidates through Nebius Token Factory.
10. The Kotlin client displays Original / A / B.
11. The user selects their preferred result.
12. The selected correction is applied non-destructively as an NLA layer in an output `.blend`.
13. Metrics and logs show latency, model usage, objective improvement and the collateral-change score.
14. The automated test suite passes.

If this does not work cleanly, **no further AI features are added.**

## Success metrics

See [METRICS.md](METRICS.md). In summary:
- time to an acceptable repair (manual vs. assisted, measured with a protocol);
- manual operations avoided;
- slip reduction;
- collateral-motion error;
- candidate acceptance rate;
- model/human A/B agreement;
- compute cost per accepted repair.

No productivity claims are made without collected evidence.

## Implementation phases

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **0: Design** | Docs, ADRs, frozen schemas, API stubs, test harness, CI, spikes defined | CI green; every deliverable in `docs/` |
| **1: Fixture + analysis** | Fixture builder, extraction, cropping, deterministic foot-slide detection | Kinesis quantifies the fixture defect at 10 cm ± tolerance |
| **2: Deterministic repair** | Numpy IK, candidates A/B, NLA layering, preservation checks, previews, golden tests | Fixture repaired reliably, golden thresholds met, no AI |
| **3: Orchestration** | DAG engine (clean-room, based on Evoco's ideas), retries, breaker, cache, fault isolation | Fault tests pass; A and B run concurrently |
| **4: Nebius / NVIDIA** | Token Factory provider, model routing, structured plan, multimodal evaluation | Contract tests pass; live test passes when the flag is set |
| **5: Review client** | Compose Desktop: job state, synced Original/A/B, metrics, decision | Definition of done, steps 10–12 |
| **6: Serverless** | Containerized worker, `NebiusJobRunner`, local vs. cloud latency and cost measured | Same golden results on both runners |
| **7: Hardening** | Fault injection, performance, UX, docs, reproducible demo | Definition of done, all 14 steps |

## Future milestones (not part of the MVP)

These come only after foot-contact cleanup is stable:

1. hand/object contact repair
2. joint jitter cleanup
3. mesh interpenetration detection
4. trajectory/arc correction
5. generalized region-of-interest repair
6. reference-video guidance
7. 2D reference motion → inferred target trajectory
8. broader pose estimation
9. static image or 2D → rough 3D asset initialization
10. Android review companion
11. Maya integration

Reference-video guidance starts small and does not attempt full video-to-3D. The pipeline is:

```
2D reference video → keypoints → target trajectory → selected-bone guidance → A/B repair
```
