# Spikes: assumptions checked before we depend on them

Each spike is a small, self-contained experiment with one question and a pass criterion. If a spike fails, the ADR that depends on it switches to its documented fallback. Results go in this file, with the date, environment and raw output.

| Spike | Question | Blocks | Status |
|---|---|---|---|
| **S1** | Does a per-candidate Action on an NLA Replace track stay bone-restricted, leave the original Action byte-identical, and survive save and reload? Does the slotted-action API work for it? | ADR 0004, Phase 2 | ✅ passes with the *pushed-original* layout; ❌ with the active-action layout |
| **S2** | Does numpy FK (rest matrices + pose basis) match Blender's `pose.bones[].matrix` within 0.1 mm? | ADR 0003, Phase 1 | ✅ 0.63 µm max error |
| **S3** | Can Workbench render headlessly at 512² in ≤ 1 s per frame, on the workstation and on GPU-less CI? | ADR 0007, `blender.yml` gating | ✅ workstation 0.057 s/frame; ✅ GPU-less CI 0.417 s/frame |
| **S4** | Token Factory: base URL, model IDs, `json_schema` support, image input, `usage` fields | ADR 0009, Phase 4 | pending (needs `NEBIUS_API_KEY`) |
| **S5** | Nebius Serverless Jobs: submit API, artifact exchange, status delivery, image startup latency, pricing | Phase 6 | pending (document only; see infra/nebius/README.md) |

## How to run

```bash
uv run task setup-blender                       # once: portable Blender 4.5.14 in .tools/
B=$(ls -d .tools/blender-4.5.14*/ | head -1)blender   # blender.exe on Windows
$B --background --factory-startup --python spikes/s1_nla_layering.py   -- --out var/spikes/s1.json
$B --background --factory-startup --python spikes/s2_fk_parity.py      -- --out var/spikes/s2.json
$B --background --factory-startup --python spikes/s3_headless_render.py -- --out var/spikes/s3.json --frames 24

uv run task models                                         # S4 Q1 (needs NEBIUS_API_KEY)
uv run python spikes/s4_token_factory.py --probe-json      # S4 Q2 (needs KINESIS_PLANNER_MODEL)
uv run python spikes/s4_token_factory.py --probe-vision    # S4 Q3 (needs KINESIS_VISION_MODEL)
```

Every Blender spike prints a JSON result. The process exits 0 on PASS and 1 on FAIL.

## Results

### S1: NLA layering
**Result: PASS, with a revised layout.** Blender 4.5.14 LTS, Windows 11, 2026-10-03.

- **Active-action layout: fails P2.** The original stays the *active action* and the candidate goes on an NLA track. The candidate is not applied (worst quaternion distance 0.198), because Blender evaluates the active action on top of the NLA stack.
- **Pushed-original layout: passes P1–P5.** A bottom NLA strip references the original, the active action is cleared, and the candidate track sits above it. Other bones are unchanged (≤1.2e-7), the candidate is exact inside its window, and the original is exact outside it. The original Action hash survives save and reload unchanged, and muting the candidate track restores the original.
- **Slotted-action API:** used to create the candidate Action.
- **Follow-up:** ADR 0004 and ARCHITECTURE §5 now specify the pushed-original layout.

### S2: FK parity
**Result: PASS.** Blender 4.5.14 LTS, Windows 11, 2026-10-03.

The test covered 60 random poses of a chain with non-zero rolls, under an armature object that was itself translated and rotated:
- **FK agreement:** the largest head/tail error between numpy FK (`M = M_parent · rest_parent⁻¹ · rest · Basis`) and Blender was **6.3e-7 m**, against a threshold of 1e-4 m.
- **Round trip:** recovering the `matrix_basis` quaternion gave a distance of 5e-8.

`kinesis.analysis` and `kinesis.repair` now use this formula. The scaled-armature case is still untested, since the MVP fixture is unscaled.

### S3: headless render
**Workstation: PASS.** Blender 4.5.14 LTS, Windows 11, NVIDIA GPU, 2026-10-03.

- **Output:** 24 of 24 frames written, 512² JPEG, Workbench.
- **Timing:** the first frame took 1.02 s (warm-up included); the mean was **0.057 s/frame**.

**CI (GitHub ubuntu-latest, no GPU, Mesa llvmpipe): PASS.** Run 37098378678 on 2026-10-03.

- **Output:** 24 of 24 frames written.
- **Timing:** the first frame took 2.83 s; the mean was **0.417 s/frame**, under the 1.0 s budget.
- **Phase 2 cost:** at this rate, a 67-frame crop plus 17 context frames per candidate takes about 35 s of CI render time.
- **S1 and S2 in the same run:** results identical to the workstation (S1 passes only with the pushed-original layout; S2 max error 6.3e-7 m), so both findings reproduce across platforms.

### S4: Token Factory
**Result: pending.** Record:
- the confirmed base URL;
- the exact model IDs;
- whether `json_schema` is supported;
- the maximum number of images per request;
- the fields present in `usage`;
- typical latency.

### S5: Serverless Jobs
**Result: pending.** Answer the questions in [infra/nebius/README.md](../infra/nebius/README.md).
