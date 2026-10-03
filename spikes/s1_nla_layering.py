"""Spike S1: does non-destructive NLA layering behave as ADR 0004 assumes?

Run it inside Blender 4.5 LTS:
    <blender> --background --factory-startup --python spikes/s1_nla_layering.py -- --out var/spikes/s1.json

The spike builds a 3-bone chain (a -> b -> c):
- the ORIGINAL action rotates all three bones over frames 1-40;
- the CANDIDATE action keys only bone "b", over frames 15-25, and is created through the
  slotted-action API when it is available (Blender 4.4 and later);
- the candidate is pushed as an NLA strip (Replace, influence 1, extrapolation NOTHING)
  on a new track. Two layouts are tested:
    ACTIVE_ORIGINAL: the original stays the active action, with the candidate track added
    PUSHED_ORIGINAL: the original is referenced by a bottom NLA strip ("Kinesis/Original"),
                     the active action is cleared, and the candidate track goes above it
  The original Action datablock is never edited in either layout.

Pass criteria (all must hold):
  P1 bones a and c evaluate exactly as the original at every frame (|delta| < 1e-6);
  P2 bone b evaluates as the candidate at frames 15-25;
  P3 bone b evaluates as the original outside 15-25;
  P4 the original action's keyframe hash is unchanged after building, saving and reloading;
  P5 muting the track restores the original pose of b at frame 20;
  P6 (info) whether the slotted API, rather than legacy action.fcurves, was used.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import tempfile
from pathlib import Path

import bpy  # type: ignore[import-not-found]
from mathutils import Quaternion  # type: ignore[import-not-found]

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
OUT = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None


def build_rig() -> bpy.types.Object:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    arm = bpy.data.armatures.new("SpikeArm")
    obj = bpy.data.objects.new("SpikeRig", arm)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    prev = None
    for i, name in enumerate("abc"):
        eb = arm.edit_bones.new(name)
        eb.head = (0.0, 0.0, float(i))
        eb.tail = (0.0, 0.0, float(i + 1))
        eb.roll = 0.3 * i  # non-zero roll, so S1 also exercises roll handling
        if prev:
            eb.parent = prev
            eb.use_connect = True
        prev = eb
    bpy.ops.object.mode_set(mode="POSE")
    for pb in obj.pose.bones:
        pb.rotation_mode = "QUATERNION"
    return obj


def key_original(obj: bpy.types.Object) -> bpy.types.Action:
    obj.animation_data_create()
    for f in range(1, 41):
        bpy.context.scene.frame_set(f)
        for i, pb in enumerate(obj.pose.bones):
            pb.rotation_quaternion = Quaternion((1, 0, 0), math.sin(f * 0.15 + i) * 0.6)
            pb.keyframe_insert("rotation_quaternion", frame=f)
    action = obj.animation_data.action
    action.name = "ORIGINAL"
    return action


def candidate_quat(f: int) -> Quaternion:
    return Quaternion((0, 1, 0), 0.9 + 0.01 * f)


def make_candidate_action(obj: bpy.types.Object) -> tuple[bpy.types.Action, bool]:
    """Create the candidate action without touching obj.animation_data.action."""
    action = bpy.data.actions.new("KIN_spike_A")
    slotted = hasattr(action, "slots") and hasattr(action, "layers")
    path = 'pose.bones["b"].rotation_quaternion'
    if slotted:
        slot = action.slots.new(id_type="OBJECT", name=obj.name)
        layer = action.layers.new("Layer")
        strip = layer.strips.new(type="KEYFRAME")
        channelbag = strip.channelbag(slot, ensure=True)
        fcurves = [channelbag.fcurves.new(path, index=i) for i in range(4)]
    else:
        fcurves = [action.fcurves.new(path, index=i, action_group="b") for i in range(4)]
    for f in range(15, 26):
        q = candidate_quat(f)
        for i in range(4):
            fcurves[i].keyframe_points.insert(f, q[i], options={"FAST"})
    for fc in fcurves:
        fc.update()
    return action, slotted


def push_original_down(obj: bpy.types.Object, original: bpy.types.Action) -> None:
    """Reference the original from a bottom NLA strip and clear the active action.

    The Action datablock itself is untouched; only the AnimData references change.
    """
    track = obj.animation_data.nla_tracks.new()
    track.name = "Kinesis/Original"
    start = int(original.frame_range[0])
    strip = track.strips.new("ORIGINAL", start, original)
    strip.blend_type = "REPLACE"
    strip.extrapolation = "HOLD"
    if hasattr(strip, "action_slot") and hasattr(original, "slots") and len(original.slots):
        strip.action_slot = original.slots[0]
    obj.animation_data.action = None


def push_track(obj: bpy.types.Object, action: bpy.types.Action) -> bpy.types.NlaTrack:
    track = obj.animation_data.nla_tracks.new()
    track.name = "Kinesis/A"
    strip = track.strips.new("KIN_A", 15, action)
    strip.blend_type = "REPLACE"
    strip.influence = 1.0
    strip.extrapolation = "NOTHING"
    strip.blend_in = strip.blend_out = 0.0
    if hasattr(strip, "action_slot") and hasattr(action, "slots") and len(action.slots):
        strip.action_slot = action.slots[0]
    return track


def action_hash(action: bpy.types.Action) -> str:
    h = hashlib.sha256()
    fcurves = []
    if hasattr(action, "layers") and len(action.layers):
        for layer in action.layers:
            for strip in layer.strips:
                for bag in getattr(strip, "channelbags", []):
                    fcurves.extend(bag.fcurves)
    if not fcurves and hasattr(action, "fcurves"):
        fcurves = list(action.fcurves)
    for fc in sorted(fcurves, key=lambda c: (c.data_path, c.array_index)):
        h.update(f"{fc.data_path}[{fc.array_index}]".encode())
        for kp in fc.keyframe_points:
            h.update(f"{kp.co[0]:.6f},{kp.co[1]:.9f};".encode())
    return h.hexdigest()


def pose_quats(obj: bpy.types.Object, f: int) -> dict[str, Quaternion]:
    bpy.context.scene.frame_set(f)
    bpy.context.view_layer.update()
    return {pb.name: pb.matrix_basis.to_quaternion() for pb in obj.pose.bones}


def qdist(p: Quaternion, q: Quaternion) -> float:
    return 1.0 - abs(p.dot(q))


def run_layout(layout: str) -> dict[str, object]:
    obj = build_rig()
    original = key_original(obj)
    frames = range(1, 41)
    baseline = {f: pose_quats(obj, f) for f in frames}
    hash_before = action_hash(original)

    cand, slotted = make_candidate_action(obj)
    if layout == "PUSHED_ORIGINAL":
        push_original_down(obj, original)
    push_track(obj, cand)

    p1 = p2 = p3 = True
    worst = {"a_c": 0.0, "b_in": 0.0, "b_out": 0.0}
    for f in frames:
        now = pose_quats(obj, f)
        for name in "ac":
            d = qdist(now[name], baseline[f][name])
            worst["a_c"] = max(worst["a_c"], d)
            p1 &= d < 1e-6
        if 15 <= f <= 25:
            d = qdist(now["b"], candidate_quat(f))
            worst["b_in"] = max(worst["b_in"], d)
            p2 &= d < 1e-6
        else:
            d = qdist(now["b"], baseline[f]["b"])
            worst["b_out"] = max(worst["b_out"], d)
            p3 &= d < 1e-6

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "s1.blend")
        bpy.ops.wm.save_as_mainfile(filepath=path)
        bpy.ops.wm.open_mainfile(filepath=path)
        p4 = action_hash(bpy.data.actions["ORIGINAL"]) == hash_before
        obj = bpy.data.objects["SpikeRig"]
        obj.animation_data.nla_tracks["Kinesis/A"].mute = True
        p5 = qdist(pose_quats(obj, 20)["b"], baseline[20]["b"]) < 1e-6

    return {
        "slotted_api": slotted,
        "P1_untouched_bones_follow_original": p1,
        "P2_candidate_inside_window": p2,
        "P3_original_outside_window": p3,
        "P4_original_hash_unchanged_after_reload": p4,
        "P5_mute_restores_original": p5,
        "worst_quat_distance": worst,
        "PASS": all((p1, p2, p3, p4, p5)),
    }


def main() -> int:
    results: dict[str, object] = {"blender_version": bpy.app.version_string}
    for layout in ("ACTIVE_ORIGINAL", "PUSHED_ORIGINAL"):
        results[layout] = run_layout(layout)
    # ADR 0004 needs at least one layout that passes; the report says which one.
    passing = [k for k in ("ACTIVE_ORIGINAL", "PUSHED_ORIGINAL") if results[k]["PASS"]]  # type: ignore[index]
    results["passing_layouts"] = passing
    results["PASS"] = bool(passing)
    text = json.dumps(results, indent=2)
    print(text)
    if OUT:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
    return 0 if results["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
