# Blender worker I/O contract (protocol v1)

The backend talks to Blender only through files in an isolated job directory. Every Pydantic model named here lives in [`backend/src/kinesis/schemas/worker.py`](../../backend/src/kinesis/schemas/worker.py). The worker runs inside Blender, where pydantic is not available. It writes plain JSON, and the **backend** validates that JSON.

## Invocation (fixed argv)

```
<KINESIS_BLENDER_BIN> --background --factory-startup -noaudio <job_dir>/input/scene.blend \
    --python-exit-code 3 --python <repo>/blender/worker/main.py -- \
    --command <inspect|extract|apply_render|export> --spec <job_dir>/work/<node>.spec.json
```

- `--factory-startup` ignores user preferences and add-ons. Auto-run Python is off in background mode, and `inspect` reports `autoexec_disabled`.
- `--python-exit-code 3` means an uncaught exception in the worker exits with status 3, which the backend reports as `WorkerCrashed`.
- The worker reads only the spec and the scene file. It writes only to paths named in the spec, which are relative to the job directory. The worker refuses absolute paths and `..`.

## Job directory layout

```
var/jobs/<job_id>/
  input/scene.blend              read-only copy of the upload
  work/<node>.spec.json          written by the backend
  work/<node>.result.json        written by the worker
  work/<node>.log                stdout+stderr captured by the runner
  candidates/<candidate_id>/candidate.blend
  candidates/<candidate_id>/frames/crop_0000.jpg ...
  candidates/<candidate_id>/frames/ctx_0000.jpg ...
  original/frames/...            the original rendered with the same cameras, for A/B
  output/<scene>_kinesis.blend   after the decision
```

## Commands

| Command | Spec | Result | Writes |
|---|---|---|---|
| `inspect` | `InspectSpec` | `InspectResult` (armatures, bones, frame range, fps, Blender version, `autoexec_disabled`) | the result only |
| `extract` | `ExtractSpec` (armature, chain bones, context frames) | `ExtractResult`: rest matrices, per-frame chain world matrices and local quaternions over the context window, world head/tail of **all** bones over the **whole** scene, plus a hash of the original action | the result only |
| `apply_render` | `ApplyRenderSpec` (keys for thigh, shin and foot; action and track names; render settings). With `render_original: true` and no keys, it renders the untouched original with the same cameras | `ApplyRenderResult`: original-action hash before and after, re-extracted all-bone samples, frame paths, `render_ms` | `candidate.blend`, frames |
| `export` | `ExportSpec` | `ExportResult` | `output/*.blend` |

On failure the worker writes a `WorkerFailure {ok: false, error_type, message}` and exits non-zero.

## NLA layout (ADR 0004)
- The original Action is referenced from a bottom strip on track `Kinesis:Original` (Replace, HOLD), and the active action is cleared. The Action datablock is never edited.
- Each candidate is a new slotted Action `KIN_<job>_<label>` holding `rotation_quaternion` keys for the keyable chain bones only, on a strip on track `Kinesis:<label>` (Replace, influence 1, extrapolation NOTHING).
- Chain bones must use quaternion rotation; otherwise the worker reports `UnsupportedRig`.
- Crop frames are `crop_0000.jpg …` (one per context frame, square). Context frames are `ctx_0000.jpg …` (every `context_every` frames, 4:3) from the scene camera.

## Conventions
- Matrices are 4×4 and row-major (`[row][col]`), matching `mathutils.Matrix` indexing.
- Quaternions are `(w, x, y, z)` in Blender order.
- Positions are world-space meters.
- Frames are integer scene frames.
- `protocol` is `1`. A change that breaks this contract bumps it, and the backend rejects any mismatch.
