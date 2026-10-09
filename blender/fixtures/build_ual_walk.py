"""Build the UAL walk scene ual_walk_slide.blend from the Quaternius Universal Animation Library.

    uv run task ual-scene -- --zip "<...>/Universal Animation Library[Standard].zip"

Source: "Universal Animation Library" (Standard, free) by Quaternius, CC0 1.0
(quaternius.itch.io/universal-animation-library). The asset itself is not committed: pass the
root-motion glTF (``UAL1_Standard_RM.glb``) from the downloaded zip.

What this script does, deterministically:
1. imports the glTF (unit scale, quaternion bones, a UE-style ``thigh_l -> calf_l -> foot_l``
   chain) and keeps only the armature and the ``Mannequin`` mesh;
2. bakes ``Walk_Loop`` (32 frames, root motion forward along -Y) into one action of
   ``CYCLES`` consecutive cycles, keyed on every frame, carrying the root forward each cycle;
3. injects a hand-made slide on the left foot during the touchdown of the third cycle:
   the ankle is pushed sideways by ``SLIDE`` (a ``synthetic.Defect`` profile) with an
   analytic two-bone IK on thigh/calf; the foot keeps its original world rotation;
4. adds the fixture's checker floor, camera and sun, and saves uncompressed.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy  # type: ignore[import-not-found]
from mathutils import Matrix, Vector  # type: ignore[import-not-found]

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_fixture  # noqa: E402  (floor, camera and light helpers)

from kinesis.testing.synthetic import Defect  # noqa: E402

SCENE_NAME = "ual_walk_slide"
SOURCE_ACTION = "Walk_Loop"
ACTION_NAME = "Armature_ual_walk_slide"
ARMATURE = "Armature"
CYCLE_FRAMES = 32
CYCLES = 4
FRAME_START = 1
FRAME_END = FRAME_START + CYCLES * CYCLE_FRAMES  # 129
START_Y = 2.6  # the walk covers about 5.2 m along -Y; start it near the far edge of the floor
FLOOR_SIZE_M = 6.0
# The left foot touches down at cycle frame 0 and its heel lifts at about cycle frame 10.
# Third cycle: frames 65-75. The slide ramps in while the foot should be still, holds until
# lift-off, then decays during the swing so the next step lands where it originally did.
SLIDE = Defect(side="L", start=66, end=74, hold_end=76, decay_end=88, dx=0.08, dy=0.0)
THIGH, CALF, FOOT = "thigh_l", "calf_l", "foot_l"


def _args() -> tuple[Path, Path]:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if "--glb" not in argv or "--out" not in argv:
        raise SystemExit("usage: blender -b -P build_ual_walk.py -- --glb <UAL1_Standard_RM.glb> --out <path.blend>")
    glb = Path(argv[argv.index("--glb") + 1]).resolve()
    if not glb.is_file():
        raise SystemExit(f"not found: {glb}")
    return glb, Path(argv[argv.index("--out") + 1]).resolve()


def import_rig(glb: Path) -> bpy.types.Object:
    bpy.ops.import_scene.gltf(filepath=str(glb))
    rig = bpy.data.objects[ARMATURE]
    for obj in list(bpy.data.objects):
        if obj is not rig and not (obj.type == "MESH" and obj.parent is rig):
            bpy.data.objects.remove(obj)
    ad = rig.animation_data_create()
    for track in list(ad.nla_tracks):
        ad.nla_tracks.remove(track)
    ad.action = None
    return rig


def bake_cycles(rig: bpy.types.Object) -> bpy.types.Action:
    """Every F-curve of the source action, sampled on each frame and repeated ``CYCLES`` times.
    Root location channels advance by one cycle's displacement per cycle."""
    src = bpy.data.actions[SOURCE_ACTION]
    action = bpy.data.actions.new(ACTION_NAME)
    rig.animation_data.action = action
    if hasattr(rig.animation_data, "action_slot") and action.slots:
        rig.animation_data.action_slot = action.slots[0]
    curves = list(src.fcurves)
    for fc in curves:
        bone = fc.data_path.split('"')[1]
        drift = 0.0
        if bone == "root" and fc.data_path.endswith(".location"):
            drift = fc.evaluate(CYCLE_FRAMES) - fc.evaluate(0)
        values = []
        for frame in range(FRAME_START, FRAME_END + 1):
            cycle, t = divmod(frame - FRAME_START, CYCLE_FRAMES)
            if frame == FRAME_END:
                cycle, t = CYCLES - 1, CYCLE_FRAMES
            values += [float(frame), fc.evaluate(t) + cycle * drift]
        rig.keyframe_insert(fc.data_path, index=fc.array_index, frame=FRAME_START, group=bone)
        out = next(
            c
            for c in _fcurves(action)
            if c.data_path == fc.data_path and c.array_index == fc.array_index
        )
        out.keyframe_points.clear()
        out.keyframe_points.add(len(values) // 2)
        out.keyframe_points.foreach_set("co", values)
        for kp in out.keyframe_points:
            kp.interpolation = "LINEAR"
        out.update()
    for other in [a for a in bpy.data.actions if a is not action]:
        bpy.data.actions.remove(other)
    return action


def _fcurves(action: bpy.types.Action) -> list[bpy.types.FCurve]:
    """F-curves of a (possibly slotted) action."""
    if getattr(action, "layers", None):
        return [fc for layer in action.layers for strip in layer.strips for bag in strip.channelbags for fc in bag.fcurves]
    return list(action.fcurves)


def _rest_axis_matrix(rot: Matrix, head: Vector) -> Matrix:
    m = rot.to_4x4()
    m.translation = head
    return m


def two_bone_ik(hip: Vector, knee: Vector, ankle: Vector, target: Vector) -> Vector:
    """The new knee: swing the leg onto the target, then re-bend it for the new hip-target
    distance, keeping the knee on the side it was on."""
    a, b = (knee - hip).length, (ankle - knee).length
    d = min((target - hip).length, a + b - 1e-6)
    u = (target - hip).normalized()
    swing = (ankle - hip).rotation_difference(target - hip)
    k1 = hip + swing @ (knee - hip)
    side = (k1 - hip) - (k1 - hip).dot(u) * u
    x = (a * a - b * b + d * d) / (2 * d)
    h = math.sqrt(max(a * a - x * x, 0.0))
    return hip + x * u + h * side.normalized()


def inject_slide(rig: bpy.types.Object) -> float:
    """Push the left ankle by ``SLIDE.offset(f)``; returns the worst ankle error (m)."""
    scene, pose = bpy.context.scene, rig.pose.bones
    world = rig.matrix_world
    inv = world.inverted()
    worst = 0.0
    for frame in range(SLIDE.start, SLIDE.decay_end + 1):
        dx, dy = SLIDE.offset(frame)
        if dx == 0.0 and dy == 0.0:
            continue
        scene.frame_set(frame)
        thigh, calf, foot = pose[THIGH], pose[CALF], pose[FOOT]
        hip = thigh.head.copy()
        knee, ankle = calf.head.copy(), foot.head.copy()
        foot_rot = foot.matrix.to_3x3()
        target = inv @ (world @ ankle + Vector((dx, dy, 0.0)))
        new_knee = two_bone_ik(hip, knee, ankle, target)
        r_thigh = (knee - hip).rotation_difference(new_knee - hip).to_matrix()
        r_calf = (r_thigh @ (ankle - knee)).rotation_difference(target - new_knee).to_matrix()
        calf_rot = r_calf @ r_thigh @ calf.matrix.to_3x3()
        thigh.matrix = _rest_axis_matrix(r_thigh @ thigh.matrix.to_3x3(), hip)
        bpy.context.view_layer.update()
        calf.matrix = _rest_axis_matrix(calf_rot, new_knee)
        bpy.context.view_layer.update()
        foot.matrix = _rest_axis_matrix(foot_rot, foot.head.copy())
        bpy.context.view_layer.update()
        for pb in (thigh, calf, foot):
            rig.keyframe_insert(f'pose.bones["{pb.name}"].rotation_quaternion', frame=frame, group=pb.name)
        worst = max(worst, (foot.head - target).length)
    return worst


def fix_quaternion_signs(action: bpy.types.Action) -> None:
    """Keep consecutive keys in the same hemisphere so linear interpolation takes the short way."""
    curves: dict[str, list[bpy.types.FCurve]] = {}
    for fc in _fcurves(action):
        if fc.data_path.endswith("rotation_quaternion"):
            curves.setdefault(fc.data_path, [None] * 4)[fc.array_index] = fc  # type: ignore[index]
    for quad in curves.values():
        points = [list(fc.keyframe_points) for fc in quad]
        for i in range(1, len(points[0])):
            dot = sum(points[c][i].co[1] * points[c][i - 1].co[1] for c in range(4))
            if dot < 0:
                for c in range(4):
                    points[c][i].co[1] = -points[c][i].co[1]
        for fc in quad:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"
            fc.update()


def main() -> int:
    glb, out = _args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    build_fixture.FLOOR_SIZE_M = FLOOR_SIZE_M
    scene = build_fixture.build_scene()
    scene.frame_start, scene.frame_end = FRAME_START, FRAME_END
    scene.render.fps, scene.render.fps_base = 24, 1.0
    scene["kinesis_fixture"] = SCENE_NAME
    scene["kinesis_source"] = "Quaternius Universal Animation Library (Standard), CC0 1.0: Walk_Loop"
    rig = import_rig(glb)
    rig.location = (0.0, START_Y, 0.0)
    bpy.context.view_layer.update()
    action = bake_cycles(rig)
    worst = inject_slide(rig)
    fix_quaternion_signs(action)
    build_fixture.build_floor(scene)
    build_fixture.build_camera_and_light(scene)
    cam = scene.camera
    cam.location = (3.2, START_Y - 2.6 + 1.0, 1.6)
    cam.rotation_euler = (Vector((0.0, START_Y - 2.6, 0.6)) - cam.location).to_track_quat("-Z", "Y").to_euler()
    scene.frame_set(scene.frame_start)
    print(f"slide: {SLIDE.magnitude_m * 100:.1f} cm on {FOOT}, frames {SLIDE.start}-{SLIDE.decay_end}; worst IK error {worst * 1000:.3f} mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=False, relative_remap=False)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
