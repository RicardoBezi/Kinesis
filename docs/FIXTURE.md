# Canonical fixture: `foot_slide_v1`

The fixture is a tiny, procedurally generated scene that can be legally redistributed (CC0). It is the regression baseline for the whole MVP:

- `blender/fixtures/build_fixture.py` builds it inside Blender 4.5 (`uv run task fixture`).
- `kinesis.testing.synthetic.foot_slide_v1()` builds the **same trajectories in pure numpy**, so unit and golden tests run without Blender.
- A Blender integration test asserts that the two agree to within 0.1 mm.

All motion is generated from closed-form functions: no randomness, no external assets.

## Scene

| Item | Value |
|---|---|
| Frame range | 1–120 |
| FPS | 24 |
| Units | metric, scale 1.0, Z-up |
| Floor | `Floor` plane, 4 × 4 m at z = 0, procedural checker material (0.1 m squares) so slip is visible |
| Character | Armature `Rig` + capsule meshes, one per bone, each parented to its bone (rigid; no skin weights) |
| Camera | `Cam_Context`, 50 mm, at (2.8, −3.2, 1.4), looking at (0, −0.3, 0.6) |
| Light | Sun, strength 3, rotation (45°, 0, 30°) |

## Rig (rest pose, world space, meters; the character faces −Y)

| Bone | Parent | Head | Tail |
|---|---|---|---|
| root | none | (0, 0, 0) | (0, 0.1, 0) |
| pelvis | root | (0, 0, 0.92) | (0, 0, 1.02) |
| spine | pelvis | (0, 0, 1.02) | (0, 0, 1.22) |
| chest | spine | (0, 0, 1.22) | (0, 0, 1.42) |
| neck | chest | (0, 0, 1.42) | (0, 0, 1.52) |
| head | neck | (0, 0, 1.52) | (0, 0, 1.72) |
| thigh.L | pelvis | (0.10, 0, 0.92) | (0.10, −0.02, 0.47) |
| shin.L | thigh.L | (0.10, −0.02, 0.47) | (0.10, 0, 0.08) |
| foot.L | shin.L | (0.10, 0, 0.08) | (0.10, −0.12, 0.02) |
| toe.L | foot.L | (0.10, −0.12, 0.02) | (0.10, −0.18, 0.02) |
| upper_arm.L | chest | (0.18, 0, 1.40) | (0.18, 0, 1.12) |
| forearm.L | upper_arm.L | (0.18, 0, 1.12) | (0.18, −0.02, 0.86) |
| hand.L | forearm.L | (0.18, −0.02, 0.86) | (0.18, −0.03, 0.78) |
| *.R | mirrored in X | | |

The bone lengths follow from the table: thigh ≈ 0.4504, shin ≈ 0.3905. The knee has a slight forward bend at rest, so the IK pole direction is well-defined.

Joint limits used by the metrics: knee flexion is in [0°, 150°].

## Motion (clean)

The clean motion is defined by three things:
- pelvis world position;
- foot targets (ankle world position plus foot world yaw);
- arm angles.

The legs are solved with the **same two-bone IK** as [ALGORITHMS.md §3.3](ALGORITHMS.md#33-procedure) and keyed as FK quaternions on every frame. The `.blend` therefore has plain FK keys and no constraints.

### Pelvis
- `y(f) = −0.6 · (f − 1) / 119`: constant forward travel of 0.6 m.
- `z(f) = 0.92 + 0.01 · sin(2π (f − 1) / 30)`: bob.
- `x(f) = 0.02 · sin(2π (f − 1) / 60)`: sway.

### Left foot (ankle target)
| Frames | Phase | Position |
|---|---|---|
| 1–19 | planted | (0.10, 0.00, 0.08) |
| 20–39 | swing | lerp (0.10, 0.00) → (0.10, −0.30) using smoothstep, with height `0.08 + 0.12·sin(π·s)` |
| 40–90 | **planted** | (0.10, −0.30, 0.08) |
| 91–110 | swing | → (0.10, −0.60), same shape |
| 111–120 | planted | (0.10, −0.60, 0.08) |

### Right foot
| Frames | Phase | Position |
|---|---|---|
| 1–64 | planted | (−0.10, −0.15, 0.08) |
| 65–84 | swing | → (−0.10, −0.45) |
| 85–120 | planted | (−0.10, −0.45, 0.08) |

Foot yaw stays constant at 0 (facing −Y).

### Arms
- Shoulder pitch: `±20° · sin(2π (f − 1) / 60)`, with left and right in opposite phase.
- Elbow: constant 15°.
- These give the RIGHT_HAND preservation invariant a moving, non-trivial target.

## Injected defect

An X offset is added to the **left foot target only**:

| Frames | `Δx(f)` |
|---|---|
| < 50 | 0 |
| 50–80 | `0.10 · ramp(f)`, linear 0 → 0.10 m, with smoothstep-rounded corners over 3 frames at each end |
| 81–90 | 0.10 (held until lift-off) |
| 91–100 | decays to 0 by smoothstep during the swing (outside any planted interval) |
| > 100 | 0 |

The rest of the rig is unaffected: pelvis, right leg, arms and spine all match the clean motion.

## Ground truth (`blender/fixtures/fixture_truth.json`)

```json
{
  "fixture": "foot_slide_v1",
  "frame_range": [1, 120],
  "fps": 24,
  "armature": "Rig",
  "target_bone": "foot.L",
  "planted_interval": [40, 90],
  "defect_window": [50, 80],
  "injected_slide_m": 0.10,
  "expected": {
    "detected_interval_tolerance_frames": 2,
    "planted_displacement_cm": {"min": 9.5, "max": 10.5},
    "severity": "MAJOR"
  },
  "selection_for_tests": {"frame_start": 45, "frame_end": 90, "context_before": 10, "context_after": 10}
}
```

## Expected repair outcomes (initial golden thresholds; recalibrated in Phase 2)

| Metric | Threshold |
|---|---|
| Best candidate `slip_reduction_pct` | ≥ 80 |
| Candidate A `slip_reduction_pct` | ≥ 95 |
| Candidate B `slip_reduction_pct` | ≥ 70 |
| `collateral_max_cm` (all candidates) | ≤ 0.01 |
| `outside_window_max_cm` | ≤ 0.001 |
| `joint_limit_violations` | 0 |
| `penetration_max_cm` | ≤ 0.5 |
| `jerk_rms_ratio` | ≤ 1.5 |
| `ik_unreachable_frames` | 0 |

Phase 2 records the measured values in `tests/golden/foot_slide_v1.json` and documents any change to these thresholds, with a reason, in [TESTING.md](TESTING.md#golden-thresholds).
