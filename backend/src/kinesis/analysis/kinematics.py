"""Rigid-body kinematics shared by analysis, repair and the fixture mirror (pure numpy).

Conventions (docs/ALGORITHMS.md, blender/worker/io_contract.md):
- matrices are 4x4 (or 3x3) and act on column vectors, indexed ``[row][col]`` like
  ``mathutils.Matrix``;
- quaternions are ``(w, x, y, z)``;
- a bone's rest matrix is Blender's ``bone.matrix_local`` (armature space). Its Y axis points
  from head to tail.

Forward kinematics follows spike S2 (parity with Blender to 6.3e-7 m)::

    M_arm(bone) = M_arm(parent) @ inv(rest(parent)) @ rest(bone) @ Basis(bone)
    M_arm(root) = rest(root) @ Basis(root)
    Basis       = T(location) @ R(quaternion)          (scale is always 1 here)

This module is also loaded by ``blender/fixtures/build_fixture.py`` inside Blender, whose
bundled interpreter is Python 3.11 with numpy 1.26. Keep it free of 3.12-only syntax and of
any import outside the standard library and numpy.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.float64]


# ---------------------------------------------------------------- quaternions


def quat_to_mat3(q: npt.ArrayLike) -> Array:
    """Rotation matrix of a quaternion (normalized first). Accepts shape (4,) or (..., 4)."""
    a = np.asarray(q, dtype=np.float64)
    a = a / np.linalg.norm(a, axis=-1, keepdims=True)
    w, x, y, z = a[..., 0], a[..., 1], a[..., 2], a[..., 3]
    m = np.empty((*a.shape[:-1], 3, 3))
    m[..., 0, 0] = 1 - 2 * (y * y + z * z)
    m[..., 0, 1] = 2 * (x * y - z * w)
    m[..., 0, 2] = 2 * (x * z + y * w)
    m[..., 1, 0] = 2 * (x * y + z * w)
    m[..., 1, 1] = 1 - 2 * (x * x + z * z)
    m[..., 1, 2] = 2 * (y * z - x * w)
    m[..., 2, 0] = 2 * (x * z - y * w)
    m[..., 2, 1] = 2 * (y * z + x * w)
    m[..., 2, 2] = 1 - 2 * (x * x + y * y)
    return m


def mat3_to_quat(m: npt.ArrayLike) -> Array:
    """Unit quaternion ``(w, x, y, z)`` with ``w >= 0`` for a 3x3 rotation matrix."""
    r = np.asarray(m, dtype=np.float64)
    tr = r[0, 0] + r[1, 1] + r[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = np.array(
            [0.25 * s, (r[2, 1] - r[1, 2]) / s, (r[0, 2] - r[2, 0]) / s, (r[1, 0] - r[0, 1]) / s]
        )
    elif r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = math.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2]) * 2
        q = np.array(
            [(r[2, 1] - r[1, 2]) / s, 0.25 * s, (r[0, 1] + r[1, 0]) / s, (r[0, 2] + r[2, 0]) / s]
        )
    elif r[1, 1] > r[2, 2]:
        s = math.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2]) * 2
        q = np.array(
            [(r[0, 2] - r[2, 0]) / s, (r[0, 1] + r[1, 0]) / s, 0.25 * s, (r[1, 2] + r[2, 1]) / s]
        )
    else:
        s = math.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1]) * 2
        q = np.array(
            [(r[1, 0] - r[0, 1]) / s, (r[0, 2] + r[2, 0]) / s, (r[1, 2] + r[2, 1]) / s, 0.25 * s]
        )
    q = q / np.linalg.norm(q)
    out: Array = -q if q[0] < 0 else q
    return out


def hemisphere_continuous(quats: npt.ArrayLike) -> Array:
    """Flip signs so that consecutive quaternions satisfy ``q[t] . q[t-1] >= 0``."""
    q = np.array(quats, dtype=np.float64)
    for t in range(1, len(q)):
        if float(np.dot(q[t], q[t - 1])) < 0:
            q[t] = -q[t]
    return q


def axis_angle_mat3(axis: npt.ArrayLike, angle: float) -> Array:
    """Right-handed rotation by ``angle`` radians about ``axis``."""
    a = np.asarray(axis, dtype=np.float64)
    a = a / np.linalg.norm(a)
    half = angle / 2
    return quat_to_mat3(np.array([math.cos(half), *(a * math.sin(half))]))


def rotation_between(a: npt.ArrayLike, b: npt.ArrayLike) -> Array:
    """``Rmin``: the minimal-arc rotation taking unit vector ``a`` onto unit vector ``b``.

    Antiparallel inputs rotate by pi about an axis perpendicular to ``a``.
    """
    u = np.asarray(a, dtype=np.float64)
    v = np.asarray(b, dtype=np.float64)
    u = u / np.linalg.norm(u)
    v = v / np.linalg.norm(v)
    k = np.cross(u, v)
    sin_angle = float(np.linalg.norm(k))
    angle = math.atan2(sin_angle, float(np.dot(u, v)))
    if sin_angle < 1e-12:
        if angle < 1.0:
            return np.eye(3)
        helper = np.array([1.0, 0.0, 0.0]) if abs(u[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        k = np.cross(u, helper)
    # Axis-angle rather than I + K + K^2 / (1 + c): stable for nearly antiparallel vectors.
    return axis_angle_mat3(k, angle)


# ---------------------------------------------------------------- matrices


def compose(rotation: npt.ArrayLike, translation: npt.ArrayLike) -> Array:
    m = np.eye(4)
    m[:3, :3] = rotation
    m[:3, 3] = translation
    return m


def basis_matrix(location: npt.ArrayLike, quat: npt.ArrayLike) -> Array:
    """Pose ``matrix_basis`` with unit scale: ``T(location) @ R(quat)``."""
    return compose(quat_to_mat3(quat), location)


def bone_rest_matrix(head: npt.ArrayLike, tail: npt.ArrayLike, roll: float = 0.0) -> Array:
    """Blender's ``bone.matrix_local`` from edit-bone head, tail and roll.

    Port of ``vec_roll_to_mat3_normalized`` (blenkernel/intern/armature.cc), including its
    special case for bones that point almost exactly along -Y.
    """
    h = np.asarray(head, dtype=np.float64)
    nor = np.asarray(tail, dtype=np.float64) - h
    nor = nor / np.linalg.norm(nor)
    x, y, z = (float(c) for c in nor)
    safe, critical = 6.1e-3, 2.5e-4
    theta = 1.0 + y
    theta_alt = x * x + z * z
    if theta > safe or theta_alt > critical * critical:
        if theta <= safe:
            theta = theta_alt * 0.5 + theta_alt * theta_alt * 0.125
        # Columns are the bone's X, Y and Z axes in armature space.
        b = np.array(
            [
                [1 - x * x / theta, x, -x * z / theta],
                [-x, y, -z],
                [-x * z / theta, z, 1 - z * z / theta],
            ]
        )
    else:
        b = np.diag([-1.0, -1.0, 1.0])
    rot = axis_angle_mat3(nor, roll) @ b
    return compose(rot, h)


# ---------------------------------------------------------------- skeleton + FK


@dataclass(frozen=True)
class Skeleton:
    """Bones in parent-before-child order, with rest matrices in armature space."""

    names: tuple[str, ...]
    parents: tuple[str | None, ...]
    rest: Mapping[str, Array]
    lengths: Mapping[str, float]

    def parent_of(self, name: str) -> str | None:
        return self.parents[self.names.index(name)]

    def offset(self, name: str) -> Array:
        """``inv(rest(parent)) @ rest(bone)``, or ``rest(bone)`` for a root bone."""
        parent = self.parent_of(name)
        if parent is None:
            return np.array(self.rest[name])
        out: Array = np.linalg.inv(self.rest[parent]) @ self.rest[name]
        return out

    @classmethod
    def from_edit_bones(
        cls, bones: Sequence[tuple[str, str | None, Sequence[float], Sequence[float], float]]
    ) -> Skeleton:
        """Build from ``(name, parent, head, tail, roll)`` rows, parents listed first."""
        seen: set[str] = set()
        for name, parent, *_ in bones:
            if parent is not None and parent not in seen:
                raise ValueError(f"bone {name!r} is listed before its parent {parent!r}")
            seen.add(name)
        return cls(
            names=tuple(b[0] for b in bones),
            parents=tuple(b[1] for b in bones),
            rest={b[0]: bone_rest_matrix(b[2], b[3], b[4]) for b in bones},
            lengths={
                b[0]: float(np.linalg.norm(np.subtract(b[3], b[2], dtype=np.float64)))
                for b in bones
            },
        )


def forward_kinematics(
    skeleton: Skeleton,
    basis: Mapping[str, Array],
    armature_world: npt.ArrayLike | None = None,
) -> dict[str, Array]:
    """World matrices per bone, shape ``(T, 4, 4)``.

    ``basis`` maps bone name to its ``(T, 4, 4)`` pose basis. Missing bones use identity.
    """
    world_obj = np.eye(4) if armature_world is None else np.asarray(armature_world, float)
    frames = next(iter(basis.values())).shape[0] if basis else 1
    identity = np.broadcast_to(np.eye(4), (frames, 4, 4))
    arm: dict[str, Array] = {}
    for name, parent in zip(skeleton.names, skeleton.parents, strict=True):
        b = basis.get(name, identity)
        offset = skeleton.offset(name)
        arm[name] = offset @ b if parent is None else arm[parent] @ offset @ b
    return {name: world_obj @ m for name, m in arm.items()}


def head_tail(world: Array, length: float) -> tuple[Array, Array]:
    """World head and tail positions, ``(T, 3)`` each, from ``(T, 4, 4)`` world matrices."""
    head = world[..., :3, 3]
    tail = head + world[..., :3, 1] * length
    return head, tail


def basis_from_world(
    skeleton: Skeleton, name: str, parent_world: Array | None, world: Array
) -> Array:
    """Invert FK for one bone: the ``Basis`` that yields ``world`` under ``parent_world``.

    ``local = (parent_world @ inv(rest_parent) @ rest)^-1 @ world`` (ALGORITHMS §3.3 step 7).
    For a root bone ``parent_world`` is the armature object's world matrix (or None).
    """
    offset = skeleton.offset(name)
    frame = offset if parent_world is None else parent_world @ offset
    out: Array = np.linalg.inv(frame) @ world
    return out


# ---------------------------------------------------------------- two-bone IK


@dataclass(frozen=True)
class TwoBoneSolution:
    knee: Array  # (3,)
    effector: Array  # T_eff = H + D * dir; equals the target unless clamped
    unreachable: bool


def solve_two_bone(
    hip: npt.ArrayLike,
    target: npt.ArrayLike,
    pole_point: npt.ArrayLike,
    upper_length: float,
    lower_length: float,
) -> TwoBoneSolution:
    """Analytic two-bone IK (ALGORITHMS §3.3 steps 7.2-7.3).

    The knee lies in the plane spanned by the hip-target direction and ``pole_point``.
    """
    h = np.asarray(hip, dtype=np.float64)
    d = np.asarray(target, dtype=np.float64) - h
    dist = float(np.linalg.norm(d))
    if dist < 1e-12:
        raise ValueError("IK target coincides with the hip")
    l1, l2 = upper_length, lower_length
    reach = 0.999 * (l1 + l2)
    unreachable = dist > reach
    clamped = min(max(dist, abs(l1 - l2) + 1e-6), reach)
    direction = d / dist
    pole = np.asarray(pole_point, dtype=np.float64) - h
    perp = pole - float(np.dot(pole, direction)) * direction
    norm = float(np.linalg.norm(perp))
    if norm < 1e-12:
        raise ValueError("IK pole is collinear with the hip-target line")
    perp /= norm
    cos_a = (l1 * l1 + clamped * clamped - l2 * l2) / (2 * l1 * clamped)
    cos_a = min(1.0, max(-1.0, cos_a))
    sin_a = math.sqrt(1.0 - cos_a * cos_a)
    knee = h + l1 * (cos_a * direction + sin_a * perp)
    return TwoBoneSolution(knee=knee, effector=h + clamped * direction, unreachable=unreachable)


def y_axis(rotation: Array) -> Array:
    """A bone's head-to-tail direction: the Y column of its world rotation."""
    out: Array = rotation[..., :3, 1]
    return out


__all__ = [
    "Array",
    "Skeleton",
    "TwoBoneSolution",
    "axis_angle_mat3",
    "basis_from_world",
    "basis_matrix",
    "bone_rest_matrix",
    "compose",
    "forward_kinematics",
    "head_tail",
    "hemisphere_continuous",
    "mat3_to_quat",
    "quat_to_mat3",
    "rotation_between",
    "solve_two_bone",
    "y_axis",
]
