"""Build the canonical regression fixture foot_slide_v1.blend (docs/FIXTURE.md).

    uv run task fixture      # calls Blender 4.5 headless with this script

Phase 0 freezes only the design and the ground truth (fixture_truth.json). The builder is
implemented in Phase 1, together with ``kinesis.testing.synthetic.foot_slide_v1()``. Both
must produce identical trajectories, using the same closed-form motion and the same
two-bone IK.

Determinism rules:
- no randomness, and every value comes from closed-form functions of the frame number;
- the leg is keyed as FK quaternions on every frame (no constraints are stored);
- the output is written with compression off, so diffs of re-generated files stay meaningful.
"""

from __future__ import annotations

import sys

if __name__ == "__main__":
    sys.stderr.write("build_fixture.py: implemented in Phase 1 (see docs/FIXTURE.md)\n")
    sys.exit(2)
