"""Kinematics primitives (ALGORITHMS §3.3) and the fixture mirror (FIXTURE.md)."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kinesis.analysis.kinematics import (
    Skeleton,
    axis_angle_mat3,
    basis_from_world,
    basis_matrix,
    bone_rest_matrix,
    forward_kinematics,
    hemisphere_continuous,
    mat3_to_quat,
    quat_to_mat3,
    rotation_between,
    solve_two_bone,
)
from kinesis.testing import synthetic

unit = st.floats(-1, 1, allow_nan=False)
quats = st.tuples(unit, unit, unit, unit).filter(lambda q: sum(c * c for c in q) > 1e-3)
vectors = st.tuples(unit, unit, unit).filter(lambda v: sum(c * c for c in v) > 1e-3)


@given(quats)
def test_quat_matrix_round_trip(q: tuple[float, float, float, float]) -> None:
    m = quat_to_mat3(q)
    assert np.allclose(m @ m.T, np.eye(3), atol=1e-12)
    assert np.linalg.det(m) == pytest.approx(1.0)
    back = mat3_to_quat(m)
    expected = np.asarray(q) / np.linalg.norm(q)
    assert abs(float(np.dot(back, expected))) == pytest.approx(1.0, abs=1e-9)
    assert back[0] >= 0


@given(vectors, vectors)
def test_rotation_between_maps_a_onto_b(a: tuple[float, ...], b: tuple[float, ...]) -> None:
    r = rotation_between(a, b)
    ua, ub = np.asarray(a) / np.linalg.norm(a), np.asarray(b) / np.linalg.norm(b)
    assert np.allclose(r @ ua, ub, atol=1e-9)
    assert np.allclose(r @ r.T, np.eye(3), atol=1e-9)


def test_rotation_between_antiparallel() -> None:
    r = rotation_between((0, 0, 1), (0, 0, -1))
    assert np.allclose(r @ np.array([0, 0, 1.0]), [0, 0, -1])


def test_rotation_between_is_minimal_arc() -> None:
    """Rmin keeps the axis perpendicular to both vectors fixed (it preserves roll)."""
    r = rotation_between((0, 1, 0), (0, 0, 1))
    assert np.allclose(r @ np.array([1.0, 0, 0]), [1, 0, 0])


def test_hemisphere_continuity() -> None:
    q = np.array([[1.0, 0, 0, 0], [-0.99, -0.1, 0, 0], [0.98, 0.2, 0, 0]])
    out = hemisphere_continuous(q)
    assert all(np.dot(out[i], out[i - 1]) >= 0 for i in range(1, len(out)))
    assert np.array_equal(q[1], [-0.99, -0.1, 0, 0])  # input not mutated


def test_rest_matrix_axes() -> None:
    m = bone_rest_matrix((1, 2, 3), (1, 2, 4))  # points +Z
    assert np.allclose(m[:3, 1], [0, 0, 1])
    assert np.allclose(m[:3, 3], [1, 2, 3])
    assert np.allclose(m[:3, :3] @ m[:3, :3].T, np.eye(3))
    rolled = bone_rest_matrix((0, 0, 0), (0, 0, 1), math.pi / 2)
    assert np.allclose(rolled[:3, 1], [0, 0, 1])
    assert not np.allclose(rolled[:3, 0], m[:3, 0])


def test_rest_matrix_pointing_down_negative_y() -> None:
    m = bone_rest_matrix((0, 0, 0), (0, -1, 0))
    assert np.allclose(m[:3, :3], np.diag([-1.0, -1.0, 1.0]))


def test_skeleton_rejects_child_before_parent() -> None:
    with pytest.raises(ValueError, match="before its parent"):
        Skeleton.from_edit_bones(
            [("b", "a", (0, 0, 0), (0, 1, 0), 0), ("a", None, (0, 0, 0), (0, 1, 0), 0)]
        )


def test_fk_rest_pose_reproduces_rest_heads() -> None:
    skel = synthetic.fixture_skeleton()
    world = forward_kinematics(skel, {"root": np.eye(4)[None]})
    for name, _parent, head, tail, _roll in synthetic.RIG_BONES:
        assert np.allclose(world[name][0, :3, 3], head)
        assert np.allclose(world[name][0, :3, 3] + world[name][0, :3, 1] * skel.lengths[name], tail)


@settings(max_examples=50)
@given(quats, st.tuples(unit, unit, unit))
def test_basis_from_world_inverts_fk(q: tuple[float, ...], loc: tuple[float, ...]) -> None:
    skel = synthetic.fixture_skeleton()
    basis = {"thigh.L": basis_matrix(loc, q)[None], "pelvis": basis_matrix((0.1, 0, 0), q)[None]}
    world = forward_kinematics(skel, basis)
    recovered = basis_from_world(skel, "thigh.L", world["pelvis"][0], world["thigh.L"][0])
    assert np.allclose(recovered, basis["thigh.L"][0], atol=1e-9)


# ---------------------------------------------------------------- two-bone IK (§3.3)


@settings(max_examples=200)
@given(
    st.floats(0.3, 0.9),
    st.floats(0.0, 2 * math.pi),
    st.floats(0.2, 0.6),
    st.floats(0.2, 0.6),
)
def test_two_bone_reaches_reachable_targets(
    frac: float, angle: float, l1: float, l2: float
) -> None:
    hip = np.array([0.1, 0.2, 0.9])
    lo, hi = abs(l1 - l2) + 1e-3, 0.999 * (l1 + l2)
    dist = lo + frac * (hi - lo)
    direction = np.array([math.sin(angle) * 0.3, math.cos(angle) * 0.3, -1.0])
    target = hip + dist * direction / np.linalg.norm(direction)
    pole = hip + np.array([0.0, -1.0, -0.4])
    sol = solve_two_bone(hip, target, pole, l1, l2)
    assert not sol.unreachable
    assert np.allclose(sol.effector, target, atol=1e-9)
    assert np.linalg.norm(sol.knee - hip) == pytest.approx(l1, abs=1e-9)
    assert np.linalg.norm(target - sol.knee) == pytest.approx(l2, abs=1e-6)
    # The knee lies in the hip-target-pole plane, on the pole's side.
    normal = np.cross(target - hip, pole - hip)
    assert abs(float(np.dot(sol.knee - hip, normal / np.linalg.norm(normal)))) < 1e-9


def test_two_bone_clamps_unreachable_and_counts() -> None:
    sol = solve_two_bone((0, 0, 1), (0, 0, -5), (0, -1, 0.5), 0.45, 0.39)
    assert sol.unreachable
    assert np.linalg.norm(sol.effector - np.array([0, 0, 1])) == pytest.approx(0.999 * 0.84)


def test_two_bone_rejects_degenerate_pole() -> None:
    with pytest.raises(ValueError, match="collinear"):
        solve_two_bone((0, 0, 1), (0, 0, 0.3), (0, 0, 0.5), 0.45, 0.39)


# ---------------------------------------------------------------- fixture mirror


@pytest.fixture(scope="module")
def scene() -> synthetic.SyntheticScene:
    return synthetic.foot_slide_v1()


def test_mirror_reaches_every_ankle_target(scene: synthetic.SyntheticScene) -> None:
    frames = range(synthetic.FRAME_START, synthetic.FRAME_END + 1)
    left = np.array([synthetic.left_ankle_target(f) for f in frames])
    right = np.array([synthetic.right_ankle_target(f) for f in frames])
    assert scene.ik_unreachable_frames == 0
    assert np.abs(scene.head("foot.L") - left).max() < 1e-9
    assert np.abs(scene.head("foot.R") - right).max() < 1e-9


def test_mirror_feet_stay_flat_and_knees_bend_forward(scene: synthetic.SyntheticScene) -> None:
    for side in "LR":
        rot = scene.world[f"foot.{side}"][:, :3, :3]
        assert np.abs(rot - scene.skeleton.rest[f"foot.{side}"][:3, :3]).max() < 1e-9
        knee_forward = (scene.head(f"shin.{side}") - scene.head(f"thigh.{side}"))[:, 1]
        hip_to_ankle = (scene.head(f"foot.{side}") - scene.head(f"thigh.{side}"))[:, 1]
        assert np.all(knee_forward < hip_to_ankle / 2)  # knee ahead (-Y) of the hip-ankle line


def test_mirror_defect_touches_only_the_left_leg(scene: synthetic.SyntheticScene) -> None:
    clean = synthetic.foot_slide_v1(defect=False)
    for name in scene.skeleton.names:
        delta = np.abs(scene.head(name) - clean.head(name)).max()
        if name in ("shin.L", "foot.L", "toe.L"):
            assert delta > 0.05, name
        else:
            assert delta < 1e-12, name


def test_mirror_arms_swing_in_opposite_phase(scene: synthetic.SyntheticScene) -> None:
    left_y = scene.head("hand.L")[:, 1] - scene.head("pelvis")[:, 1]
    right_y = scene.head("hand.R")[:, 1] - scene.head("pelvis")[:, 1]
    assert np.ptp(left_y) > 0.1
    assert np.corrcoef(left_y, right_y)[0, 1] < -0.9


def test_defect_profile_matches_spec() -> None:
    d = synthetic.defect_offset_x
    assert d(49) == 0.0
    assert d(50) == 0.0
    assert d(80) == pytest.approx(0.10)
    assert all(d(f) == pytest.approx(0.10) for f in range(80, 91))
    assert d(100) == pytest.approx(0.0)
    assert d(101) == 0.0
    ramp = [d(f) for f in range(50, 81)]
    assert all(b >= a for a, b in itertools.pairwise(ramp))
    steps = np.diff(ramp)
    assert steps[10:20] == pytest.approx([0.10 / 27] * 10)  # linear middle section


def test_local_quaternions_are_unit_and_continuous(scene: synthetic.SyntheticScene) -> None:
    for q in scene.local_quat.values():
        assert np.allclose(np.linalg.norm(q, axis=1), 1.0)
        assert np.all(np.einsum("ij,ij->i", q[1:], q[:-1]) >= 0)


def test_axis_angle_sign_convention() -> None:
    r = axis_angle_mat3((1, 0, 0), math.radians(90))
    assert np.allclose(r @ np.array([0, 0, -1.0]), [0, 1, 0])
