"""Kinesis Blender add-on (stub).

The panel is planned for Phase 5/7:
- pick the armature, the target bones (from the pose-mode selection) and the frame range
  (from the timeline preview range or markers);
- pick the contact object (the floor by default) and write an optional instruction;
- "Measure & Repair" saves a copy of the .blend, POSTs it to /v1/scenes and then /v1/jobs,
  and opens the review client;
- "Import Selected Repair" links the chosen candidate Action from the output .blend and adds
  it as an NLA track. It never edits the active action.

The add-on does no repair math and makes no AI calls (ADR 0003).
"""

bl_info = {
    "name": "Kinesis",
    "author": "Kinesis contributors",
    "version": (0, 1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Kinesis",
    "description": "Localized animation repair: measure, propose, compare, apply",
    "category": "Animation",
}


def register() -> None:
    pass


def unregister() -> None:
    pass
