"""Build the canonical regression fixture foot_slide_v1.blend (docs/FIXTURE.md).

    uv run task fixture      # calls Blender 4.5 headless with this script

The motion comes from ``kinesis.testing.synthetic`` (loaded from the repository, pure numpy),
so the .blend and the numpy mirror share one source of truth. Blender then evaluates the
keys with its own FK, and ``tests/integration/test_blender_worker.py`` checks that result
against the mirror to within 0.1 mm.

Determinism rules:
- no randomness: every value comes from closed-form functions of the frame number;
- every bone is keyed as an FK quaternion on every frame (no constraints are stored), and
  the pelvis also gets location keys;
- the output is written with compression off, so diffs of re-generated files stay meaningful.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy  # type: ignore[import-not-found]
from mathutils import Euler, Matrix, Vector  # type: ignore[import-not-found]

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from kinesis.testing import synthetic  # noqa: E402  (pure numpy; needs the path above)

FIXTURE_NAME = "foot_slide_v1"
ACTION_NAME = "Rig_foot_slide_v1"
FLOOR_SIZE_M = 4.0
CHECKER_M = 0.1

# Capsule radius per bone (meters). Bones not listed (root) get no mesh.
RADIUS = {
    "pelvis": 0.11,
    "spine": 0.10,
    "chest": 0.12,
    "neck": 0.04,
    "head": 0.09,
    "thigh": 0.06,
    "shin": 0.045,
    "foot": 0.035,
    "toe": 0.03,
    "upper_arm": 0.04,
    "forearm": 0.035,
    "hand": 0.03,
}


def _args() -> tuple[Path, str]:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if "--out" not in argv:
        raise SystemExit("usage: blender -b -P build_fixture.py -- --out <path.blend> [--variant NAME]")
    variant = argv[argv.index("--variant") + 1] if "--variant" in argv else "foot_slide_v1"
    if variant not in synthetic.VARIANTS:
        raise SystemExit(f"unknown variant {variant!r}; choose from {sorted(synthetic.VARIANTS)}")
    return Path(argv[argv.index("--out") + 1]).resolve(), variant


def _material(name: str, rgb: tuple[float, float, float]) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)  # what Workbench shows with color_type MATERIAL
    return mat


def build_scene() -> bpy.types.Scene:
    scene = bpy.context.scene
    scene.name = "Scene"
    scene.frame_start, scene.frame_end = synthetic.FRAME_START, synthetic.FRAME_END
    scene.render.fps, scene.render.fps_base = synthetic.FPS, 1.0
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    scene["kinesis_fixture"] = FIXTURE_NAME
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "MATERIAL"
    return scene


def build_floor(scene: bpy.types.Scene) -> None:
    """A grid of 0.1 m quads with two alternating materials: slip is visible in Workbench."""
    n = round(FLOOR_SIZE_M / CHECKER_M)
    half = FLOOR_SIZE_M / 2
    verts = [
        (-half + i * CHECKER_M, -half + j * CHECKER_M, 0.0) for j in range(n + 1) for i in range(n + 1)
    ]
    faces = [
        (j * (n + 1) + i, j * (n + 1) + i + 1, (j + 1) * (n + 1) + i + 1, (j + 1) * (n + 1) + i)
        for j in range(n)
        for i in range(n)
    ]
    mesh = bpy.data.meshes.new("Floor")
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(_material("Floor_Light", (0.8, 0.8, 0.8)))
    mesh.materials.append(_material("Floor_Dark", (0.25, 0.25, 0.28)))
    for poly in mesh.polygons:
        i, j = poly.index % n, poly.index // n
        poly.material_index = (i + j) % 2
    mesh.update()
    scene.collection.objects.link(bpy.data.objects.new("Floor", mesh))


def build_rig(scene: bpy.types.Scene) -> bpy.types.Object:
    data = bpy.data.armatures.new("Rig")
    rig = bpy.data.objects.new("Rig", data)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    for name, parent, head, tail, roll in synthetic.RIG_BONES:
        eb = data.edit_bones.new(name)
        eb.head, eb.tail, eb.roll = head, tail, roll
        eb.use_deform = True
        if parent is not None:
            eb.parent = data.edit_bones[parent]
            eb.use_connect = False
    bpy.ops.object.mode_set(mode="OBJECT")
    for pb in rig.pose.bones:
        pb.rotation_mode = "QUATERNION"
    return rig


def _capsule(name: str, length: float, radius: float, sides: int = 12) -> bpy.types.Mesh:
    """A tapered cylinder along +Y from 0 to ``length`` (bone-local), closed at both ends."""
    rings = [(0.0, 0.55), (0.12, 1.0), (0.88, 1.0), (1.0, 0.55)]
    verts: list[tuple[float, float, float]] = []
    for frac, scale in rings:
        y = frac * length
        for k in range(sides):
            a = 2 * math.pi * k / sides
            verts.append((radius * scale * math.cos(a), y, radius * scale * math.sin(a)))
    faces: list[tuple[int, ...]] = []
    for r in range(len(rings) - 1):
        for k in range(sides):
            a, b = r * sides + k, r * sides + (k + 1) % sides
            faces.append((a, b, b + sides, a + sides))
    faces.append(tuple(range(sides - 1, -1, -1)))
    faces.append(tuple(range((len(rings) - 1) * sides, len(rings) * sides)))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh


def build_body(scene: bpy.types.Scene, rig: bpy.types.Object) -> None:
    left = _material("Body_Left", (0.85, 0.45, 0.2))
    right = _material("Body_Right", (0.2, 0.5, 0.85))
    center = _material("Body_Center", (0.7, 0.7, 0.65))
    for bone in rig.data.bones:
        radius = RADIUS.get(bone.name.split(".")[0])
        if radius is None:
            continue
        mesh = _capsule(f"Body_{bone.name}", bone.length, radius)
        side = bone.name.rsplit(".", 1)[-1] if "." in bone.name else ""
        mesh.materials.append({"L": left, "R": right}.get(side, center))
        obj = bpy.data.objects.new(f"Body_{bone.name}", mesh)
        scene.collection.objects.link(obj)
        obj.parent = rig
        obj.parent_type = "BONE"
        obj.parent_bone = bone.name
        # Bone parenting is relative to the bone's tail; undo that so the mesh's local frame
        # is the bone frame (head at the origin, +Y along the bone).
        obj.matrix_parent_inverse = Matrix.Translation((0.0, -bone.length, 0.0))


def key_motion(rig: bpy.types.Object, scene_data: synthetic.SyntheticScene) -> bpy.types.Action:
    """Write every key directly into F-curves (slotted-action API in Blender 4.4+)."""
    action = bpy.data.actions.new(ACTION_NAME)
    rig.animation_data_create()
    slotted = hasattr(action, "slots") and hasattr(action, "layers")
    if slotted:
        slot = action.slots.new(id_type="OBJECT", name=rig.name)
        strip = action.layers.new("Layer").strips.new(type="KEYFRAME")
        bag = strip.channelbag(slot, ensure=True)

        def new_curve(path: str, index: int, group: str) -> bpy.types.FCurve:
            fc = bag.fcurves.new(path, index=index)
            fc.group = bag.groups.get(group) or bag.groups.new(group)
            return fc

    else:

        def new_curve(path: str, index: int, group: str) -> bpy.types.FCurve:
            return action.fcurves.new(path, index=index, action_group=group)

    frames = list(range(scene_data.frame_start, scene_data.frame_end + 1))
    channels: list[tuple[str, str, int, list[float]]] = []
    for name in scene_data.skeleton.names:
        q = scene_data.local_quat[name]
        for i in range(4):
            channels.append((name, "rotation_quaternion", i, [float(v) for v in q[:, i]]))
    for i in range(3):
        loc = [float(v) for v in scene_data.pelvis_location[:, i]]
        channels.append(("pelvis", "location", i, loc))

    for bone, prop, index, values in channels:
        fc = new_curve(f'pose.bones["{bone}"].{prop}', index, bone)
        fc.keyframe_points.add(len(frames))
        flat = [c for f, v in zip(frames, values, strict=True) for c in (float(f), v)]
        fc.keyframe_points.foreach_set("co", flat)
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
        fc.update()

    rig.animation_data.action = action
    if slotted and hasattr(rig.animation_data, "action_slot"):
        rig.animation_data.action_slot = action.slots[0]
    return action


def build_camera_and_light(scene: bpy.types.Scene) -> None:
    cam_data = bpy.data.cameras.new("Cam_Context")
    cam_data.lens = 50
    cam = bpy.data.objects.new("Cam_Context", cam_data)
    cam.location = (2.8, -3.2, 1.4)
    direction = Vector((0.0, -0.3, 0.6)) - cam.location
    cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(cam)
    scene.camera = cam

    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 3.0
    sun = bpy.data.objects.new("Sun", sun_data)
    sun.rotation_euler = Euler((math.radians(45), 0.0, math.radians(30)))
    scene.collection.objects.link(sun)


def main() -> int:
    out, variant = _args()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = build_scene()
    scene["kinesis_fixture"] = variant
    build_floor(scene)
    rig = build_rig(scene)
    build_body(scene, rig)
    data = synthetic.variant_scene(variant)
    if data.ik_unreachable_frames:
        raise RuntimeError(f"fixture has {data.ik_unreachable_frames} unreachable IK frames")
    key_motion(rig, data)
    build_camera_and_light(scene)
    scene.frame_set(scene.frame_start)
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=False, relative_remap=False)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
