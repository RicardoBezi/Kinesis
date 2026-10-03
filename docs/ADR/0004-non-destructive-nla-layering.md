# ADR 0004: Non-destructive repair via a new Action on an NLA track

**Status:** Accepted, pending spike S1

## Context
The animator must be able to compare a repair with the original and revert it, without any risk to the source animation.

## Decision
- **The source file is never written.** The uploaded `.blend` is copied into the job directory, and the copy is never written either.
- **Each candidate is its own Action.** The Action is named `KIN_<job>_<label>`. It holds keys only for the chain bones, and only within `[a−k, b+k]`.
- **Candidates sit on an NLA track.** Each Action is pushed as a strip on track `Kinesis/<label>`, with blend type Replace and influence 1. The track sits above the original Action.
- **Export.** The `export` command writes `output/<scene>_kinesis.blend`. That file contains the original Action unchanged, with the chosen track enabled.
- **Tests prove the original is untouched.** Integration tests hash every F-curve keyframe of the original Action before and after, and compare the hashes.

## Assumptions to verify (S1)
- **Bone fallthrough.** In Blender 4.5, a Replace strip whose Action has no channels for a bone leaves that bone evaluating from the lower layer (the active action).
- **No effect outside the strip.** Keys outside the strip's frame range do not affect evaluation. Strip extrapolation is set to `NOTHING`, and blend-in/out stays at 0 because the blending is baked into the keys.
- **Slotted-action API is enough.** The 4.5 slotted-action API (`action.slots`, `action.layers[].strips[].channelbag(slot)`) can create these Actions without the legacy `action.fcurves` path. This keeps the worker forward-compatible with Blender 5.x, which removes the legacy path.

## Fallback
If NLA evaluation does not behave as assumed, each candidate is stored as a standalone Action, and an add-on operator swaps the active action. This is still non-destructive, but less convenient for comparison.
