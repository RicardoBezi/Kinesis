"""Numpy mirror of the canonical fixture ``foot_slide_v1`` (docs/FIXTURE.md).

``blender/fixtures/build_fixture.py`` loads this very file inside Blender and keys the local
quaternions and pelvis locations it computes, so the motion has one source of truth. The
Blender integration test then checks that Blender's own evaluation of those keys agrees
with :func:`foot_slide_v1` to within 0.1 mm, which independently verifies the rest matrices
and FK in ``kinesis.analysis.kinematics``.

Like ``kinematics``, this module must stay importable by Blender's Python 3.11 / numpy 1.26
and must not import pydantic or anything outside numpy and the standard library.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from kinesis.analysis.kinematics import (
    Array,
    Skeleton,
    axis_angle_mat3,
    basis_from_world,
    compose,
    forward_kinematics,
    hemisphere_continuous,
    mat3_to_quat,
    rotation_between,
    solve_two_bone,
    y_axis,
)

FRAME_START = 1
FRAME_END = 120
FPS = 24
ARMATURE = "Rig"

# (name, parent, head, tail, roll), rest pose in world space; the character faces -Y.
_LEFT = (
    ("thigh.L", "pelvis", (0.10, 0.0, 0.92), (0.10, -0.02, 0.47)),
    ("shin.L", "thigh.L", (0.10, -0.02, 0.47), (0.10, 0.0, 0.08)),
    ("foot.L", "shin.L", (0.10, 0.0, 0.08), (0.10, -0.12, 0.02)),
    ("toe.L", "foot.L", (0.10, -0.12, 0.02), (0.10, -0.18, 0.02)),
    ("upper_arm.L", "chest", (0.18, 0.0, 1.40), (0.18, 0.0, 1.12)),
    ("forearm.L", "upper_arm.L", (0.18, 0.0, 1.12), (0.18, -0.02, 0.86)),
    ("hand.L", "forearm.L", (0.18, -0.02, 0.86), (0.18, -0.03, 0.78)),
)


def _mirror(name: str) -> str:
    return name[:-2] + ".R" if name.endswith(".L") else name


def _mirror_x(v: tuple[float, float, float]) -> tuple[float, float, float]:
    return (-v[0], v[1], v[2])


RIG_BONES: tuple[tuple[str, str | None, tuple[float, ...], tuple[float, ...], float], ...] = (
    ("root", None, (0.0, 0.0, 0.0), (0.0, 0.1, 0.0), 0.0),
    ("pelvis", "root", (0.0, 0.0, 0.92), (0.0, 0.0, 1.02), 0.0),
    ("spine", "pelvis", (0.0, 0.0, 1.02), (0.0, 0.0, 1.22), 0.0),
    ("chest", "spine", (0.0, 0.0, 1.22), (0.0, 0.0, 1.42), 0.0),
    ("neck", "chest", (0.0, 0.0, 1.42), (0.0, 0.0, 1.52), 0.0),
    ("head", "neck", (0.0, 0.0, 1.52), (0.0, 0.0, 1.72), 0.0),
    *((n, p, h, t, 0.0) for n, p, h, t in _LEFT),
    *((_mirror(n), _mirror(p), _mirror_x(h), _mirror_x(t), 0.0) for n, p, h, t in _LEFT),
)
"""Edit bones of the fixture rig, parents listed before children."""

LEG_CHAINS = {
    "L": ("thigh.L", "shin.L", "foot.L"),
    "R": ("thigh.R", "shin.R", "foot.R"),
}

# Motion constants (docs/FIXTURE.md § Motion).
PELVIS_BASE_Z = 0.86
PELVIS_BOB_M = 0.01
PELVIS_SWAY_M = 0.02
TRAVEL_M = 0.6
ANKLE_PLANTED_Z = 0.08
SWING_HEIGHT_M = 0.12
SHOULDER_PITCH_DEG = 20.0
ELBOW_DEG = 15.0
KNEE_POLE_OFFSET = np.array([0.0, -1.0, 0.0])
"""IK pole for the fixture legs: 1 m in front of the hip, so knees always bend forward.
(The rest knee is not usable as a pole: when the foot is ahead of the hip it lies behind the
hip-ankle line and the knee would flip backwards.)"""

# Each foot: planted positions (x, y) and the swings between them as (last planted frame,
# first planted frame). A swing runs over the frames strictly between those two.
LEFT_STEPS = (((0.10, 0.0), (0.10, -0.30), (0.10, -0.60)), ((19, 40), (90, 111)))
RIGHT_STEPS = (((-0.10, -0.15), (-0.10, -0.45)), ((64, 85),))

# Injected defect (docs/FIXTURE.md § Injected defect).
DEFECT_SLIDE_M = 0.10
DEFECT_START, DEFECT_END = 50, 80
DEFECT_HOLD_END = 90
DEFECT_DECAY_END = 100
DEFECT_CORNER_FRAMES = 3


def smoothstep(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def rounded_ramp(x: float, corner: float) -> float:
    """0 -> 1 on [0, 1]: linear in the middle, constant-acceleration blends of width ``corner``.

    Equivalent to integrating a trapezoidal velocity profile, so position and velocity are
    both continuous.
    """
    x = min(1.0, max(0.0, x))
    vmax = 1.0 / (1.0 - corner)
    if x < corner:
        return vmax * x * x / (2 * corner)
    if x > 1.0 - corner:
        return 1.0 - vmax * (1.0 - x) ** 2 / (2 * corner)
    return vmax * (x - corner / 2)


@dataclass(frozen=True)
class Defect:
    """An injected slide on one foot's ankle target: ramps in over ``start..end``, holds until
    ``hold_end`` (lift-off), decays during the swing until ``decay_end``."""

    side: str = "L"
    start: int = DEFECT_START
    end: int = DEFECT_END
    hold_end: int = DEFECT_HOLD_END
    decay_end: int = DEFECT_DECAY_END
    dx: float = DEFECT_SLIDE_M
    dy: float = 0.0
    corner_frames: int = DEFECT_CORNER_FRAMES

    @property
    def magnitude_m(self) -> float:
        return math.hypot(self.dx, self.dy)

    def weight(self, frame: int) -> float:
        if frame < self.start or frame > self.decay_end:
            return 0.0
        if frame <= self.end:
            span = self.end - self.start
            return rounded_ramp((frame - self.start) / span, self.corner_frames / span)
        if frame <= self.hold_end:
            return 1.0
        return 1 - smoothstep((frame - self.hold_end) / (self.decay_end - self.hold_end))

    def offset(self, frame: int) -> tuple[float, float]:
        w = self.weight(frame)
        return (self.dx * w, self.dy * w)


CANONICAL_DEFECT = Defect()


@dataclass(frozen=True)
class Variant:
    """A benchmark scene: the canonical motion with one injected defect and its selection."""

    name: str
    defect: Defect
    bone: str
    selection: tuple[int, int]


VARIANTS: dict[str, Variant] = {
    v.name: v
    for v in (
        Variant("foot_slide_v1", CANONICAL_DEFECT, "foot.L", (45, 90)),
        # 3 cm backward (+Y: the character faces -Y), near the MINOR/MAJOR boundary.
        Variant("small_backward", Defect(dx=0.0, dy=0.03), "foot.L", (45, 90)),
        # The right foot, planted on frames 1-64; the slide decays during its swing (65-84).
        Variant(
            "right_foot",
            Defect(side="R", start=20, end=50, hold_end=64, decay_end=74, dx=-0.06),
            "foot.R",
            (15, 60),
        ),
        # A slower diagonal slide across almost the whole contact.
        Variant("long_diagonal", Defect(start=42, end=88, dx=0.06, dy=-0.06), "foot.L", (45, 90)),
    )
}


def defect_offset_x(frame: int) -> float:
    """``Δx(f)`` added to the left ankle target of the canonical fixture."""
    return CANONICAL_DEFECT.offset(frame)[0]


def pelvis_position(frame: int) -> tuple[float, float, float]:
    u = frame - FRAME_START
    return (
        PELVIS_SWAY_M * math.sin(2 * math.pi * u / 60),
        -TRAVEL_M * u / (FRAME_END - FRAME_START),
        PELVIS_BASE_Z + PELVIS_BOB_M * math.sin(2 * math.pi * u / 30),
    )


def ankle_target(
    steps: tuple[tuple[tuple[float, float], ...], tuple[tuple[int, int], ...]], frame: int
) -> tuple[float, float, float]:
    """Clean ankle target for one foot: planted between swings, smoothstep arcs during them."""
    plants, swings = steps
    for i, (lift, land) in enumerate(swings):
        if frame <= lift:
            x, y = plants[i]
            return (x, y, ANKLE_PLANTED_Z)
        if frame < land:
            s = (frame - lift) / (land - lift)
            k = smoothstep(s)
            (x0, y0), (x1, y1) = plants[i], plants[i + 1]
            z = ANKLE_PLANTED_Z + SWING_HEIGHT_M * math.sin(math.pi * s)
            return (x0 + (x1 - x0) * k, y0 + (y1 - y0) * k, z)
    x, y = plants[-1]
    return (x, y, ANKLE_PLANTED_Z)


def left_ankle_target(
    frame: int, *, defect: bool = True, injected: Defect | None = CANONICAL_DEFECT
) -> tuple[float, float, float]:
    x, y, z = ankle_target(LEFT_STEPS, frame)
    if defect and injected is not None and injected.side == "L":
        dx, dy = injected.offset(frame)
        return (x + dx, y + dy, z)
    return (x, y, z)


def right_ankle_target(frame: int, *, injected: Defect | None = None) -> tuple[float, float, float]:
    x, y, z = ankle_target(RIGHT_STEPS, frame)
    if injected is not None and injected.side == "R":
        dx, dy = injected.offset(frame)
        return (x + dx, y + dy, z)
    return (x, y, z)


def shoulder_pitch_rad(frame: int) -> float:
    """Left shoulder pitch; the right arm uses the opposite sign."""
    return math.radians(SHOULDER_PITCH_DEG) * math.sin(2 * math.pi * (frame - FRAME_START) / 60)


@dataclass(frozen=True)
class SyntheticScene:
    """A fully evaluated synthetic animation.

    - ``basis``: per-bone pose basis, ``(T, 4, 4)``; what Blender stores as keys.
    - ``local_quat``: per-bone ``(T, 4)`` quaternions, hemisphere-continuous.
    - ``pelvis_location``: ``(T, 3)`` pose-space location keys of the pelvis.
    - ``world``: per-bone ``(T, 4, 4)`` world matrices from FK.
    """

    skeleton: Skeleton
    frame_start: int
    fps: float
    basis: dict[str, Array]
    local_quat: dict[str, Array]
    pelvis_location: Array
    world: dict[str, Array]
    armature: str = ARMATURE
    ik_unreachable_frames: int = 0

    @property
    def frame_count(self) -> int:
        return int(self.pelvis_location.shape[0])

    @property
    def frame_end(self) -> int:
        return self.frame_start + self.frame_count - 1

    def head(self, bone: str) -> Array:
        """World head positions ``(T, 3)``."""
        out: Array = self.world[bone][:, :3, 3]
        return out

    def tail(self, bone: str) -> Array:
        out: Array = self.head(bone) + self.world[bone][:, :3, 1] * self.skeleton.lengths[bone]
        return out


def fixture_skeleton() -> Skeleton:
    return Skeleton.from_edit_bones(RIG_BONES)


def foot_slide_v1(*, defect: bool = True) -> SyntheticScene:
    """The canonical fixture over frames 1-120. ``defect=False`` gives the clean motion."""
    return build_scene(CANONICAL_DEFECT if defect else None)


def variant_scene(name: str) -> SyntheticScene:
    return build_scene(VARIANTS[name].defect)


def build_scene(injected: Defect | None) -> SyntheticScene:
    """The canonical motion with ``injected`` (or no) defect."""
    skel = fixture_skeleton()
    frames = range(FRAME_START, FRAME_END + 1)
    n = len(frames)
    rest_rot = {name: skel.rest[name][:3, :3] for name in skel.names}
    rest_head = {name: skel.rest[name][:3, 3] for name in skel.names}

    eye = np.broadcast_to(np.eye(4), (n, 4, 4)).copy()
    basis: dict[str, Array] = {name: eye.copy() for name in skel.names}

    # Pelvis: translation only. With a translation-only basis, world head = rest head + R @ loc.
    pelvis_world = np.array([pelvis_position(f) for f in frames])
    pelvis_loc = (pelvis_world - rest_head["pelvis"]) @ rest_rot["pelvis"]  # R^T @ delta per row
    for t in range(n):
        basis["pelvis"][t] = compose(np.eye(3), pelvis_loc[t])

    # Arms: world rotation about world X at the shoulder; the forearm adds a fixed elbow bend
    # (negative angle about X swings the hand forward, towards -Y); the hand follows the forearm.
    x_axis = np.array([1.0, 0.0, 0.0])
    world = forward_kinematics(skel, basis)
    for side, sign in (("L", 1.0), ("R", -1.0)):
        upper, fore = f"upper_arm.{side}", f"forearm.{side}"
        chest = world["chest"]
        for t, f in enumerate(frames):
            pitch = sign * shoulder_pitch_rad(f)
            r_upper = axis_angle_mat3(x_axis, pitch) @ rest_rot[upper]
            basis[upper][t] = _rotation_basis(skel, upper, chest[t], r_upper)
        world = forward_kinematics(skel, basis)
        for t, f in enumerate(frames):
            pitch = sign * shoulder_pitch_rad(f)
            r_fore = axis_angle_mat3(x_axis, pitch - math.radians(ELBOW_DEG)) @ rest_rot[fore]
            basis[fore][t] = _rotation_basis(skel, fore, world[upper][t], r_fore)

    # Legs: two-bone IK onto the ankle targets, keyed as FK. Feet keep their rest world
    # rotation (yaw 0, flat).
    world = forward_kinematics(skel, basis)
    unreachable = 0
    targets = {
        "L": [left_ankle_target(f, injected=injected) for f in frames],
        "R": [right_ankle_target(f, injected=injected) for f in frames],
    }
    for side, (thigh, shin, foot) in LEG_CHAINS.items():
        l1, l2 = skel.lengths[thigh], skel.lengths[shin]
        pelvis_w = world["pelvis"]
        for t in range(n):
            thigh_frame = pelvis_w[t] @ skel.offset(thigh)  # thigh world at identity basis
            hip = thigh_frame[:3, 3]
            sol = solve_two_bone(hip, targets[side][t], hip + KNEE_POLE_OFFSET, l1, l2)
            unreachable += int(sol.unreachable)
            r_thigh0 = thigh_frame[:3, :3]
            r_thigh = rotation_between(y_axis(r_thigh0), sol.knee - hip) @ r_thigh0
            basis[thigh][t] = _rotation_basis(skel, thigh, pelvis_w[t], r_thigh)
            thigh_w = pelvis_w[t] @ skel.offset(thigh) @ basis[thigh][t]
            shin_frame = thigh_w @ skel.offset(shin)
            r_shin0 = shin_frame[:3, :3]
            r_shin = rotation_between(y_axis(r_shin0), sol.effector - sol.knee) @ r_shin0
            basis[shin][t] = _rotation_basis(skel, shin, thigh_w, r_shin)
            shin_w = thigh_w @ skel.offset(shin) @ basis[shin][t]
            basis[foot][t] = _rotation_basis(skel, foot, shin_w, rest_rot[foot])

    world = forward_kinematics(skel, basis)
    local_quat = {
        name: hemisphere_continuous(np.array([mat3_to_quat(b[:3, :3]) for b in basis[name]]))
        for name in skel.names
    }
    return SyntheticScene(
        skeleton=skel,
        frame_start=FRAME_START,
        fps=float(FPS),
        basis=basis,
        local_quat=local_quat,
        pelvis_location=pelvis_loc,
        world=world,
        ik_unreachable_frames=unreachable,
    )


def _rotation_basis(skel: Skeleton, name: str, parent_world: Array, rotation: Array) -> Array:
    """Rotation-only basis that gives ``name`` the world ``rotation`` under ``parent_world``."""
    # Only the rotation part is kept: with no basis translation the head stays where FK puts
    # it, so connected joints stay connected.
    b = basis_from_world(skel, name, parent_world, compose(rotation, np.zeros(3)))
    return compose(b[:3, :3], np.zeros(3))


__all__ = [
    "ARMATURE",
    "FPS",
    "FRAME_END",
    "FRAME_START",
    "LEG_CHAINS",
    "RIG_BONES",
    "VARIANTS",
    "Defect",
    "SyntheticScene",
    "build_scene",
    "defect_offset_x",
    "fixture_skeleton",
    "foot_slide_v1",
    "left_ankle_target",
    "pelvis_position",
    "right_ankle_target",
    "rounded_ramp",
    "smoothstep",
    "variant_scene",
]
