# ADR 0003: Pure-numpy core, thin Blender worker

**Status:** Accepted. Spike S2 passed on 2026-10-03: max joint error 6.3e-7 m against a 1e-4 m threshold (Blender 4.5.14)

## Context
Blender's Python API only exists inside a Blender process. Installing Blender in every CI job is slow, and rendering may need a GPU. If the repair ran through Blender constraints (an IK constraint, then a bake), the logic would be hard to unit-test and would depend on Blender behaviour that changes between versions.

## Decision
- **The core math is pure numpy.** Detection, cropping, two-bone IK, candidate generation and metrics live in `kinesis.analysis`, `kinesis.repair` and `kinesis.evaluation`. These modules never import `bpy`, fastapi, redis or httpx, and a test enforces this.
- **The Blender worker is thin.** It has four commands:
  - **inspect:** reads armatures, the bone hierarchy, rest matrices and the frame range.
  - **extract:** reads per-frame local pose and world matrices.
  - **apply_render:** writes the given local rotations as keys into a new Action, re-extracts the result and renders frames.
  - **export:** writes the output `.blend`.
- **A numpy mirror of the fixture.** `kinesis.testing.synthetic` reproduces the trajectories of the canonical fixture, so the golden tests run in normal CI without Blender.

## Consequences
- **Fast tests.** Unit and golden tests are fast and deterministic on any OS.
- **Risk: FK parity.** The numpy forward kinematics must match Blender's pose evaluation, including rest matrices, bone roll and parent-relative transforms. Spike S2 must show agreement within 0.1 mm before Phase 2 depends on it.
- **Fallback.** If parity can't be reached, numpy computes the IK targets and Blender's IK constraint solves them before baking. The interfaces stay the same.
