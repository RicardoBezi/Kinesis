# Kinesis: Algorithms (frozen for the MVP)

This document specifies the deterministic core well enough that it can be implemented and tested without improvisation. It is versioned as `algorithm_version = "foot_contact/1"`. Any change to a formula or default bumps that version, because the version feeds into candidate IDs.

## Conventions

- **Units.** Blender world space, Z-up, in **meters**. Metrics are reported in **centimeters** (`_cm`), and the conversion happens only at reporting time.
- **Frames.** Integers, inclusive ranges. `fps` comes from the scene.
- **Arrays.** Arrays are `float64` and per-frame arrays have shape `(T, 3)`. Index `i` maps to frame `f0 + i`.
- **Scope.** The input to every algorithm is the **context window** `[frame_start − context_before, frame_end + context_after]`, clipped to the scene range.
- **Purity.** Every function is pure: inputs are never mutated (property tests enforce this), and no randomness is used.

## 1. Scope resolution (cropping)

### 1.1 Temporal crop
```
context_start = max(scene_start, frame_start - context_before)   # default context 10
context_end   = min(scene_end,   frame_end   + context_after)
```

Validation:
- `frame_end ≥ frame_start`;
- the requested range lies inside the scene range;
- `(context_end − context_start + 1) ≤ 600`.

### 1.2 Skeletal crop
Given `target_bones`, for example `["foot.L"]`, and the armature hierarchy:

| Field | Contents | Example |
|---|---|---|
| `chain_bones` | Each target, plus its ancestors up to 2 levels above the leg root, plus its direct children and their descendants (for example the `ball_leaf_l` end bone of game rigs) | thigh.L, shin.L, foot.L, toe.L |
| `context_bones` | The ancestors above the chain up to the armature root. Read-only, used for hip position and root motion | pelvis, root |
| Keyable bones | The chain minus the end effector's children | thigh.L, shin.L, foot.L. `toe.L` follows its parent and is not keyed |

The MVP recognizes the leg chain by walking parents from the target until it finds a bone with two descendants in a chain (thigh → shin → foot). If the walk does not find a thigh → shin → foot chain, the request is rejected with `UNSUPPORTED_RIG` (422).

### 1.3 Global sampling for verification
Extraction always returns world-space head and tail positions for **all** deform bones at **every** frame of the full scene range. This is cheap: 21 bones × 120 frames for the fixture. Preservation metrics (§4) use these to check that a candidate leaves the rest of the animation intact: optimize locally, verify globally.

## 2. Foot-slide detection

**Inputs**
- `p[t]`: world position of the foot head (the ankle joint).
- `q[t]`: world position of the toe head (the ball of the foot).
- Contact plane `(o, n)` with `|n| = 1`. For the floor this defaults to `o = (0,0,0)` and `n = (0,0,1)`.
- `fps`.

**DetectionConfig defaults:** `H = 0.025 m`, `V = 0.6 m/s`, `floor_percentile = 5`, `gap_close = 2 frames`, `min_run = 6 frames`, `consistency_band = 0.01 m`.

1. **Height:** `h[t] = min(dot(p[t] − o, n), dot(q[t] − o, n))`
2. **Horizontal projection:** `P(x) = x − dot(x − o, n)·n`, and `u[t] = P(p[t])`
3. **Horizontal speed** (central difference, one-sided at the ends):
   - `s[t] = ‖u[t+1] − u[t−1]‖ · fps / 2` for interior frames
   - `s[0] = ‖u[1] − u[0]‖ · fps`
   - `s[T−1] = ‖u[T−1] − u[T−2]‖ · fps`
4. **Floor height:** `h_floor = percentile(h, floor_percentile)` over the context window.
5. **Contact candidates:** `c[t] = (h[t] − h_floor ≤ H) and (s[t] ≤ V)`
   - `V` is deliberately far above typical slide speed (10 cm over 30 frames at 24 fps ≈ 0.08 m/s) and far below swing speed (> 1.5 m/s). A *sliding* planted foot therefore still counts as planted, which is exactly the case we need to catch.
6. **Clean the mask:**
   1. Close interior gaps: false runs of length ≤ `gap_close` that are bounded by true on both sides.
   2. Remove true runs shorter than `min_run`.
7. **Intervals:** take the maximal true runs `[a, b]`, and keep only those that intersect `[frame_start, frame_end]`.
8. **Per-interval metrics:**
   - `anchor = u[a]` (onset position)
   - `planted_displacement_cm = 100 · max_{t∈[a,b]} ‖u[t] − u[a]‖` ← the **headline metric**
   - `path_slip_cm = 100 · Σ_{t=a+1..b} ‖u[t] − u[t−1]‖`
   - `max_frame_slip_cm = 100 · max_{t=a+1..b} ‖u[t] − u[t−1]‖`
   - `mean_slip_velocity_cm_s = 100 · mean_{t∈[a,b]} s[t]`
   - `velocity_variance = var_{t∈[a,b]} s[t]` (m²/s²)
   - `contact_consistency = mean_{t∈[a,b]} [ |h[t] − median(h[a..b])| ≤ consistency_band ]`
9. **Worst interval:** the one with the largest `planted_displacement_cm`.
10. **Severity** of the worst interval:

    | Severity | `planted_displacement_cm` |
    |---|---|
    | NONE | < 0.5 |
    | MINOR | < 2.0 |
    | MAJOR | ≥ 2.0 |

    If severity is NONE, the job completes with "no defect detected" and no candidates.

The `DetectionConfig` that was used is embedded in the `DefectReport`.

## 3. Candidate repair

The repair operates on the worst interval `[a, b]`. Let `k = blend_frames`.

### 3.1 Parameters (`CandidateParameters`, hard bounds enforced by the schema)
| Field | Type / bounds | Meaning |
|---|---|---|
| `lock_strength` | float [0, 1] | Fraction of the slip removed |
| `anchor_mode` | `ONSET` \| `MEAN` | Lock to the touchdown position, or to the mean planted position |
| `blend_frames` | int [0, 24] | Smoothstep ramp length outside `[a, b]` |
| `tolerance_cm` | float [0, 5] | Residual offset below which the foot snaps exactly to the anchor |
| `lock_yaw` | bool | Hold the foot's world yaw at the onset value during contact |
| `smoothing_window` | int ∈ {0,3,5,7,9} | Centered moving-average window on the correction deltas |
| `height_clamp` | bool | Prevent the foot from going below `h_floor` |

### 3.2 Preset candidates
| | **A: strong lock** | **B: soft, preserving** |
|---|---|---|
| `lock_strength` | 1.0 | 0.85 |
| `anchor_mode` | ONSET | MEAN |
| `blend_frames` | 3 | 8 |
| `tolerance_cm` | 0.2 | 1.0 |
| `lock_yaw` | true | false |
| `smoothing_window` | 0 | 5 |
| `height_clamp` | true | true |

On the fixture, B's arithmetic works out as follows. The MEAN anchor sits about 5 cm along the slide. With strength 0.85, the corrected endpoints sit about 4.25 cm and 5.75 cm from the original onset, which leaves about 1.5 cm of displacement (roughly 85% reduction). B trades residual slip for less deviation from the original trajectory.

### 3.3 Procedure

1. **Anchor:**
   - ONSET: `anchor = u[a]`
   - MEAN: `anchor = mean(u[a..b])`
2. **Envelope:**
   - `env[t] = 1` for `t ∈ [a, b]`
   - `env[t] = smoothstep((t − (a − k)) / k)` for `t ∈ [a − k, a)`
   - `env[t] = smoothstep(((b + k) − t) / k)` for `t ∈ (b, b + k]`
   - `env[t] = 0` everywhere else, where `smoothstep(x) = 3x² − 2x³`. When `k = 0` there are no ramps.
   - The envelope is clipped to the context window.
3. **Weights:** `w[t] = lock_strength · env[t]`
4. **Horizontal correction:** `δ[t] = w[t] · (anchor − u[t])`
   - If `smoothing_window = m > 0`, replace `δ` with its centered moving average (window `m`, edge-padded), then multiply by `env[t] > 0` so frames outside the window stay exactly 0.
   - Tolerance snap: for `t ∈ [a, b]`, if `‖u[t] + δ[t] − anchor‖ < tolerance_cm/100`, set `δ[t] = anchor − u[t]`.
5. **Target ankle position:** `p'[t] = p[t] + δ[t]`. If `height_clamp` is set, raise `p'[t]` along `n` until `h'[t] ≥ h_floor`. Here `h'` is computed with the toe offset carried rigidly from the original foot orientation.
6. **Target foot world rotation:**
   - If `lock_yaw`: decompose `R_foot[t]` into its yaw about `n` and the residual tilt. Set the yaw to `slerp(yaw[t], yaw[a], w[t])` and keep the tilt.
   - Otherwise: `R'_foot[t] = R_foot[t]`.
7. **Two-bone IK** (thigh `L1`, shin `L2`, lengths from the rest pose). For each `t` with `w[t] > 0`:
   1. Hip `H` = the thigh head world position, taken from the **unmodified** pelvis, which preserves root motion.
   2. Direction and distance: `d = T − H`, `D = clamp(‖d‖, |L1 − L2| + 1e-6, 0.999·(L1 + L2))`. If clamping occurred because `‖d‖ > 0.999·(L1 + L2)`, increment `ik_unreachable_frames`.
   3. Knee position:
      - `dir = d/‖d‖`
      - `pole = K_orig[t] − H`, where `K_orig` is the original knee world position
      - `perp = normalize(pole − dot(pole, dir)·dir)`
      - `cos α = (L1² + D² − L2²) / (2·L1·D)`
      - `K = H + L1·(cos α · dir + sin α · perp)`
   4. Thigh world rotation `R'_thigh = Rmin(dir_thigh_orig → normalize(K − H)) · R_thigh_orig`. `Rmin` is the minimal-arc rotation between two unit vectors, which preserves roll.
   5. Shin world rotation `R'_shin = Rmin(dir_shin_orig → normalize(T_eff − K)) · R_shin_orig`, where `T_eff = H + D·dir`.
   6. Foot world rotation: `R'_foot` from step 6.
   7. Convert to pose-local rotations with `local = (parent_world · rest_offset)⁻¹ · world`. This uses the rest matrices exported by `extract`, and parity is verified by spike S2. Write quaternions, enforcing hemisphere continuity (`q[t] · q[t−1] ≥ 0`).
8. **Output:** local quaternions for `thigh`, `shin` and `foot` on frames where `w[t] > 0` only. No location or scale keys are written, and no other bone is touched.

**Invariants** (property-tested):
- Frames with `w[t] = 0` produce no keys.
- Bones outside the keyable set produce no keys.
- The output is a pure function of `(features, parameters, algorithm_version)`.

### 3.4 Candidate identity
```
input_hash   = sha256(canonical_json(scope) || sha256(features_artifact_bytes))
candidate_id = sha256(input_hash || canonical_json(parameters) || algorithm_version)[:16]   # hex
```
`canonical_json` means sorted keys, no whitespace, floats as `repr`. Identical inputs and parameters always produce the same ID, which gives idempotency and a cache key.

## 4. Objective metrics (`CandidateMetrics`)

Every metric is computed from **original** and **candidate** world samples that the worker re-extracts after applying the candidate. All slip metrics are measured on the **original** interval `[a, b]`. The interval is not re-detected, so a candidate cannot game the detection.

| Metric | Definition |
|---|---|
| `planted_displacement_cm_before` | §2 step 8 on the original `u` over `[a, b]` |
| `planted_displacement_cm_after` | The same formula on candidate `u'` over `[a, b]` |
| `slip_reduction_pct` | `100 · (before − after) / before`. Reported as 0 when `before < 0.5 cm` |
| `contact_error_cm` | `100 · RMS_{t∈[a,b]} ‖u'[t] − mean(u'[a..b])‖`: how far the planted foot wanders |
| `penetration_max_cm` | `100 · max_t max(0, h_floor − h'[t])` over the context window |
| `jerk_rms_ratio` | `RMS(j') / max(RMS(j), 1e-9)`, where `j = Δ³p · fps³` (third forward difference of the ankle world position) over `[a − k − 3, b + k + 3]` ∩ context |
| `joint_limit_violations` | Count of frames where the candidate's knee flexion angle falls outside `[0°, 150°]` **and** the original's does not |
| `target_deviation_rms_cm` | `100 · RMS` of world-position deltas of the knee, ankle and toe joints over `[a − k, b + k]` |
| `collateral_max_cm` | `100 · max` over **all frames of the full scene** and **all bones not in the chain** of the world head/tail delta |
| `outside_window_max_cm` | `100 · max` over frames **outside** `[a − k, b + k]` and **all bones** of the world head/tail delta |
| `root_deviation_cm` | `100 · max` over all frames of the world head delta of `root` and `pelvis` |
| `ik_unreachable_frames` | From §3.3 step 7 |

### 4.1 Objective ranking

Some candidates are **gated** (ranked last and flagged). A candidate is gated if any of these hold:
- `collateral_max_cm > 0.01`
- `outside_window_max_cm > 0.001`
- `joint_limit_violations > 0`
- `penetration_max_cm > 0.5`

Every other candidate is scored as:
```
score = 0.55 · clip(slip_reduction_pct / 100, 0, 1)
      + 0.20 · (1 − clip(jerk_rms_ratio − 1, 0, 1))
      + 0.15 · (1 − clip(target_deviation_rms_cm / 5, 0, 1))
      + 0.10 · (1 − clip(contact_error_cm / 2, 0, 1))
```
Ties break by lower `target_deviation_rms_cm`, then by label. The weights are recorded in the `EvaluationReport`. The objective ranking is advisory, just like the model's ranking.

## 5. Visual crop camera

1. Compute the axis-aligned bounding box of the ankle and toe world positions over `[a − k, b + k]` (context-clipped), expanded by `margin = 0.35 m`.
2. Place the camera at `center + (0.9, −1.6, 0.6)·r`, where `r = max(extent)`, aimed at `center` with a 35 mm focal length. Use an orthographic fallback if `r < 0.1`.
3. Render the crop view at 512×512, one frame per context frame, as JPEG at quality 90 with the Workbench engine.
4. The context camera is a fixed wide shot of the whole character from the fixture camera, rendered every 4th frame.

Parameters are recorded in `render_config_hash`.
