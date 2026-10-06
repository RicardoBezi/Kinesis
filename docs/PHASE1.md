# Phase 1: fixture and analysis

**Status: complete (2026-10-05).** Kinesis measures the injected foot slide on the canonical fixture as **10.0 cm while planted on frames 39–91** (truth: 10 cm on 40–90, tolerance ±2 frames and 9.5–10.5 cm). The measurement holds on the numpy mirror in `ci.yml` and on the real `.blend` through headless Blender in `blender.yml`.

## Deliverables

| Deliverable | Where | Tests |
|---|---|---|
| Kinematics core: quaternions, Blender rest matrices, FK and its inverse, `Rmin`, two-bone IK | [`kinesis/analysis/kinematics.py`](../backend/src/kinesis/analysis/kinematics.py) | `tests/unit/test_kinematics.py` (hypothesis) |
| Numpy mirror of the fixture | [`kinesis/testing/synthetic.py`](../backend/src/kinesis/testing/synthetic.py) | `test_kinematics.py`, golden |
| Fixture builder and committed `foot_slide_v1.blend` | [`blender/fixtures/`](../blender/fixtures/) | `test_blender_worker.py::test_committed_fixture_matches_builder` |
| Scope resolution (temporal clip, thigh → shin → foot chain) | [`kinesis/analysis/scope.py`](../backend/src/kinesis/analysis/scope.py) | `tests/unit/test_scope_resolution.py` |
| Foot-slide detection, ALGORITHMS §2 | [`kinesis/analysis/detection.py`](../backend/src/kinesis/analysis/detection.py) | `tests/unit/test_detection.py`, golden |
| Worker `inspect` and `extract` | [`blender/worker/main.py`](../blender/worker/main.py) | `tests/integration/test_blender_worker.py` |
| `LocalJobRunner`, spec/result I/O | [`kinesis/jobs/local.py`](../backend/src/kinesis/jobs/local.py), [`worker_io.py`](../backend/src/kinesis/jobs/worker_io.py) | `tests/unit/test_worker_io.py`, Blender tests |
| Extract → detection adapter | [`kinesis/analysis/extraction.py`](../backend/src/kinesis/analysis/extraction.py) | `test_worker_io.py`, Blender tests |
| Fake `ExtractResult` from the mirror (for Phase 3's `FakeJobRunner`) | [`kinesis/testing/fake_worker.py`](../backend/src/kinesis/testing/fake_worker.py) | `test_worker_io.py` |

## Measured results

| Check | Result | Threshold |
|---|---|---|
| Detected interval on the fixture | [39, 91] | [40, 90] ± 2 |
| Planted displacement | 10.00 cm | 9.5–10.5 cm |
| Same selection on the clean motion | 0.40 cm, severity NONE | < 0.5 cm |
| Blender evaluation of the fixture keys vs the mirror | 3.7e-7 m | < 1e-4 m |
| Numpy rest matrix vs Blender `matrix_local` (300 random bones) | 1.1e-5 typical; 2e-4 worst, only for bones within 0.3° of −Y, from float32 storage | n/a: extract ships Blender's own rest matrices |
| Blender test wall time (inspect, 2× extract, rebuild, scaled-rig check) | about 6 s on the workstation | n/a |

## Design changes made during the phase

1. **The pelvis is lowered from 0.92 m to 0.86 m.** At 0.92 the hip-to-ankle distance was 0.84 m against a leg length of 0.8409 m, so every frame with a horizontal hip-to-foot offset was unreachable. The worst frame now sits at 95% of full extension. FIXTURE.md has the new formula.
2. **The fixture IK pole is a point 1 m in front of the hip**, not the rest knee. With the rest knee as the pole, the knee flipped backwards whenever the foot was ahead of the hip. A unit test caught this before any `.blend` existed. The *repair* procedure (ALGORITHMS §3.3) keeps the original knee as its pole, which is correct there because the original knee already bends the right way.
3. **The swing and defect formulas are now exact.** The swing parameter is `s = (f − f_lift) / (f_land − f_lift)`. The defect ramp uses constant-acceleration corners, defined in FIXTURE.md. The spec previously described both only in words.
4. **The motion has one source of truth.** The Blender builder imports `kinesis.testing.synthetic` (pure numpy, Python 3.11-compatible) and keys what it computes, rather than reimplementing the formulas in `bpy`. Blender's independent FK evaluation is what the integration test compares.
5. **`Rmin` uses an `atan2` axis-angle form.** The `I + K + K²/(1 + c)` form lost 1e-6 of accuracy for nearly antiparallel vectors (found by hypothesis).
6. **Scaled armatures are rejected** with `UNSUPPORTED_RIG` (concern C7). The worker refuses them in `extract`, and the backend maps the failure without leaking the traceback.
7. **The scene upload and defect routes move to Phase 3.** They need the SQLite `JobStore`, which ADR 0005 and the phase plan place in Phase 3. The domain logic behind them (`inspect`, scope resolution, detection) is done.

## Carried forward

- **C3:** make `blender.yml` a required check once it has a week of green nightly runs. This is an owner action in GitHub branch protection.
- **Phase 2** starts from `ExtractedMotion` and `Detection.floor_height`. It implements ALGORITHMS §3 (candidates A/B through `solve_two_bone`), §4 (metrics) and the worker's `apply_render` and `export`.
