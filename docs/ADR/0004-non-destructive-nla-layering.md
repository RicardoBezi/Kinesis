# ADR 0004: Non-destructive repair via a new Action on an NLA track

**Status:** Accepted. Revised after spike S1 on 2026-10-03, which disproved the first layout

## Context
The animator must be able to compare a repair with the original and revert it, without any risk to the source animation.

## Decision
- **The source file is never written.** The uploaded `.blend` is copied into the job directory, and the copy is never written either.
- **Each candidate is its own Action.** The Action is named `KIN_<job>_<label>`. It holds keys only for the chain bones, and only within `[a−k, b+k]`.
- **The original moves to an NLA strip; its data stays untouched.** In the job's copy, a bottom NLA strip on track `Kinesis:Original` (Replace, extrapolation HOLD) references the original Action, and the active action slot is cleared. The Action datablock is not modified; only the AnimData references change.
- **Candidates sit on NLA tracks above it.** Each candidate Action is pushed as a strip on track `Kinesis:<label>` (Replace, influence 1, extrapolation NOTHING).
- **Export.** The `export` command writes `output/<scene>_kinesis.blend`. That file contains the original Action unchanged, with the chosen track enabled.
- **Tests prove the original is untouched.** Integration tests hash every F-curve keyframe of the original Action before and after, and compare the hashes.

## Naming note (Phase 2)
Track names use a colon (`Kinesis:Original`, `Kinesis:A`) rather than the slash used in spike S1, because worker contract names are `BlenderName`s, which exclude `/` so that names stay safe in logs and file names.

## S1 result (Blender 4.5.14 LTS, Windows, 2026-10-03)

| Layout | P1 other bones untouched | P2 candidate applied | P3 original outside window | P4 original hash after reload | P5 mute restores |
|---|---|---|---|---|---|
| Original stays the **active action**, candidate track added | ✅ | ❌ (quat distance 0.198) | ✅ | ✅ | ✅ |
| Original **pushed to a bottom NLA strip**, active action cleared | ✅ | ✅ (0.0) | ✅ | ✅ | ✅ |

Blender evaluates the active action **on top of** the NLA stack, so in the first layout the original overrode the candidate on the bones being repaired. Creating candidate Actions through the slotted-action API (`slots`, `layers`, `strips`, `channelbag`) worked, so the legacy `action.fcurves` path is not needed.

## Original assumptions (kept for the record)
- **Bone fallthrough.** In Blender 4.5, a Replace strip whose Action has no channels for a bone leaves that bone evaluating from the lower layer (the active action).
- **No effect outside the strip.** Keys outside the strip's frame range do not affect evaluation. Strip extrapolation is set to `NOTHING`, and blend-in/out stays at 0 because the blending is baked into the keys.
- **Slotted-action API is enough.** The 4.5 slotted-action API (`action.slots`, `action.layers[].strips[].channelbag(slot)`) can create these Actions without the legacy `action.fcurves` path. This keeps the worker forward-compatible with Blender 5.x, which removes the legacy path.

## Fallback
If NLA evaluation does not behave as assumed, each candidate is stored as a standalone Action, and an add-on operator swaps the active action. This is still non-destructive, but less convenient for comparison.
