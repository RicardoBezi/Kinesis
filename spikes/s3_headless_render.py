"""Spike S3: can Blender render Workbench frames headlessly, and how fast?

ADR 0007 depends on this. Run it inside Blender 4.5 LTS:
    <blender> --background --factory-startup --python spikes/s3_headless_render.py -- --out var/spikes/s3.json [--frames 24]

The spike builds a small scene (floor plane, a few capsules, a camera, a sun) and renders
N frames at 512x512 to JPEG with BLENDER_WORKBENCH. It records the wall time per frame and
checks that every file exists and is non-empty.

Pass: all frames are written, and the mean time is <= 1.0 s per frame on the machine under
test. Record the result per environment (Windows workstation, GitHub ubuntu-latest).
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import bpy  # type: ignore[import-not-found]

argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
OUT = Path(argv[argv.index("--out") + 1]) if "--out" in argv else None
FRAMES = int(argv[argv.index("--frames") + 1]) if "--frames" in argv else 24


def build_scene() -> None:
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    bpy.ops.mesh.primitive_plane_add(size=4)
    for i in range(4):
        bpy.ops.mesh.primitive_cylinder_add(radius=0.05, depth=0.4, location=(0.1 * i, 0, 0.3))
        cyl = bpy.context.active_object
        cyl.keyframe_insert("location", frame=1)
        cyl.location.x += 0.3
        cyl.keyframe_insert("location", frame=FRAMES)
    bpy.ops.object.camera_add(location=(1.2, -1.6, 0.8), rotation=(1.2, 0, 0.6))
    scene.camera = bpy.context.active_object
    bpy.ops.object.light_add(type="SUN", rotation=(0.8, 0, 0.5))
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.light = "STUDIO"
    scene.display.shading.color_type = "OBJECT"
    scene.render.resolution_x = scene.render.resolution_y = 512
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "JPEG"
    scene.render.image_settings.quality = 90
    scene.frame_start, scene.frame_end = 1, FRAMES


def main() -> int:
    build_scene()
    scene = bpy.context.scene
    timings = []
    with tempfile.TemporaryDirectory() as tmp:
        written = 0
        for f in range(1, FRAMES + 1):
            scene.frame_set(f)
            scene.render.filepath = str(Path(tmp) / f"frame_{f:04d}.jpg")
            t0 = time.perf_counter()
            bpy.ops.render.render(write_still=True)
            timings.append(time.perf_counter() - t0)
            p = Path(scene.render.filepath)
            written += int(p.exists() and p.stat().st_size > 0)
    mean = sum(timings) / len(timings)
    results = {
        "blender_version": bpy.app.version_string,
        "platform": sys.platform,
        "frames_requested": FRAMES,
        "frames_written": written,
        "first_frame_s": round(timings[0], 3),
        "mean_s_per_frame": round(mean, 3),
        "max_s_per_frame": round(max(timings), 3),
        "PASS": written == FRAMES and mean <= 1.0,
    }
    text = json.dumps(results, indent=2)
    print(text)
    if OUT:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(text)
    return 0 if results["PASS"] else 1


if __name__ == "__main__":
    sys.exit(main())
