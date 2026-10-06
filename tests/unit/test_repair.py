"""Candidate repair steps (ALGORITHMS §3.3) on the fixture mirror and small inputs."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest

from kinesis.analysis.detection import ContactPlane
from kinesis.repair.candidates import (
    RepairInput,
    envelope,
    generate_candidate,
    moving_average,
    repair_input,
)
from kinesis.schemas import DEFAULT_CANDIDATES, AnchorMode, CandidateLabel, CandidateParameters
from kinesis.testing.fixture_pipeline import LEFT_LEG, FixtureRun, run_fixture

A = DEFAULT_CANDIDATES[CandidateLabel.A]


@pytest.fixture(scope="module")
def run() -> FixtureRun:
    return run_fixture()


def _params(**kw: object) -> CandidateParameters:
    return A.model_copy(update=kw)


def test_envelope_ramps_and_clips() -> None:
    env = envelope(20, 100, (105, 110), 3)
    assert env[5:11].tolist() == [1.0] * 6
    assert env[:2].tolist() == [0.0, 0.0]  # frames 100, 101: before a - k = 102
    assert 0 < env[3] < env[4] < 1  # 103, 104 ramp up (102 is the zero point)
    assert env[2] == 0.0
    assert 0 < env[12] < env[11] < 1
    assert env[13:].tolist() == [0.0] * 7
    assert envelope(5, 0, (0, 4), 8).tolist() == [1.0] * 5  # ramps fall outside the window
    assert envelope(10, 0, (3, 5), 0).tolist() == [0, 0, 0, 1, 1, 1, 0, 0, 0, 0]


def test_envelope_is_symmetric() -> None:
    env = envelope(40, 0, (15, 24), 6)
    assert np.allclose(env[:20], env[39:19:-1])


def test_moving_average_edge_padded() -> None:
    v = np.array([[0.0], [0.0], [3.0], [0.0], [0.0]])
    assert moving_average(v, 3)[:, 0].tolist() == pytest.approx([0, 1, 1, 1, 0])
    assert moving_average(v, 0)[:, 0].tolist() == v[:, 0].tolist()
    ramp = np.arange(6.0)[:, None]
    assert moving_average(ramp, 5)[0, 0] == pytest.approx((0 + 0 + 0 + 1 + 2) / 5)


def test_candidate_a_locks_the_ankle_on_the_onset(run: FixtureRun) -> None:
    c = run.candidates[CandidateLabel.A]
    a, b = run.interval
    fs = run.scene.frame_start
    ankle = c.scene.head("foot.L")[a - fs : b - fs + 1]
    assert np.abs(ankle[:, :2] - ankle[0, :2]).max() < 1e-9
    assert np.allclose(ankle[0], run.scene.head("foot.L")[a - fs])


def test_ik_effector_matches_applied_fk(run: FixtureRun) -> None:
    """The keys, evaluated by FK, put the ankle exactly where the IK said."""
    for c in run.candidates.values():
        lo, hi = c.result.window or (0, -1)
        fs, ctx = run.scene.frame_start, run.motion.context_range[0]
        applied = c.scene.head("foot.L")[lo - fs : hi - fs + 1]
        assert np.abs(applied - c.result.effector[lo - ctx : hi - ctx + 1]).max() < 1e-9


def test_strength_zero_reproduces_the_original(run: FixtureRun) -> None:
    """With no lock and no tolerance snap (step 4 snaps even at strength 0), IK is exact."""
    zero = run_fixture(
        {CandidateLabel.A: _params(lock_strength=0.0, lock_yaw=False, tolerance_cm=0.0)}
    )
    c = zero.candidates[CandidateLabel.A]
    assert np.abs(c.scene.head("foot.L") - run.scene.head("foot.L")).max() < 1e-9
    assert np.abs(c.scene.head("shin.L") - run.scene.head("shin.L")).max() < 1e-9


def test_mean_anchor_sits_between_onset_and_end(run: FixtureRun) -> None:
    mean = run_fixture({CandidateLabel.A: _params(anchor_mode=AnchorMode.MEAN)})
    c = mean.candidates[CandidateLabel.A]
    a = run.interval[0] - run.scene.frame_start
    x_locked = c.scene.head("foot.L")[a, 0]
    assert 0.1 < x_locked < 0.2


def test_quaternions_are_unit_and_hemisphere_continuous(run: FixtureRun) -> None:
    for c in run.candidates.values():
        for _frames, q in c.result.keys.values():
            assert np.allclose(np.linalg.norm(q, axis=1), 1.0)
            assert np.all(np.einsum("ij,ij->i", q[1:], q[:-1]) >= 0)


def test_unreachable_target_is_clamped_and_counted(run: FixtureRun) -> None:
    """A downward contact normal makes the height clamp push the ankle 0.5 m below the floor,
    out of the leg's reach."""
    inp = _input(run, floor=run.detection.floor_height)
    flipped = replace(inp, plane=ContactPlane(normal=(0.0, 0.0, -1.0)), floor_height=0.5)
    result = generate_candidate(flipped, A)
    assert result.ik_unreachable_frames > 0
    hip = inp.world["thigh.L"][:, :3, 3]
    a = run.interval[0] - inp.frame_start
    upper = np.linalg.norm(inp.world["shin.L"][a, :3, 3] - hip[a])
    lower = np.linalg.norm(inp.world["foot.L"][a, :3, 3] - inp.world["shin.L"][a, :3, 3])
    reach = np.linalg.norm(result.effector - hip, axis=1)
    assert reach.max() <= 0.999 * (upper + lower) + 1e-9


def test_height_clamp_raises_targets_below_the_floor(run: FixtureRun) -> None:
    inp = _input(run, floor=0.05)
    clamped = generate_candidate(inp, A)
    free = generate_candidate(inp, _params(height_clamp=False))
    active = clamped.envelope > 0
    assert np.all(clamped.target_ankle[active, 2] >= free.target_ankle[active, 2] - 1e-12)
    assert clamped.target_ankle[active, 2].min() > free.target_ankle[active, 2].min()


def test_keys_reproduce_the_target_foot_rotation(run: FixtureRun) -> None:
    for c in run.candidates.values():
        lo, hi = c.result.window or (0, -1)
        fs, ctx = run.scene.frame_start, run.motion.context_range[0]
        applied = c.scene.world["foot.L"][lo - fs : hi - fs + 1, :3, :3]
        target = c.result.foot_rotation[lo - ctx : hi - ctx + 1]
        assert np.abs(applied - target).max() < 1e-9


def _yaw(r: np.ndarray) -> float:
    return math.atan2(float(r[1, 1]), float(r[0, 1]))


def test_lock_yaw_holds_the_onset_heading(run: FixtureRun) -> None:
    """A foot that yaws during contact is turned back to its onset heading; tilt is kept."""
    inp = _input(run, floor=run.detection.floor_height)
    world = {k: v.copy() for k, v in inp.world.items()}
    count = world["foot.L"].shape[0]
    for i in range(count):  # yaw up to 20 degrees about +Z over the window
        angle = math.radians(20) * i / (count - 1)
        c, s = math.cos(angle), math.sin(angle)
        rz = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        world["foot.L"][i, :3, :3] = rz @ world["foot.L"][i, :3, :3]
    yawed = replace(inp, world=world)
    a, b = (f - inp.frame_start for f in run.interval)
    locked = generate_candidate(yawed, A).foot_rotation
    free = generate_candidate(yawed, _params(lock_yaw=False)).foot_rotation
    for t in range(a, b + 1):
        assert _yaw(locked[t]) == pytest.approx(_yaw(world["foot.L"][a, :3, :3]), abs=1e-9)
        assert locked[t][2, 1] == pytest.approx(world["foot.L"][t, 2, 1], abs=1e-12)  # tilt
        assert np.allclose(free[t], world["foot.L"][t, :3, :3])


def _input(run: FixtureRun, *, floor: float) -> RepairInput:
    return repair_input(run.motion, LEFT_LEG, run.interval, floor)


def test_interval_outside_window_rejected(run: FixtureRun) -> None:
    inp = repair_input(run.motion, LEFT_LEG, (10, 20), run.detection.floor_height)
    with pytest.raises(ValueError, match="outside the context"):
        generate_candidate(inp, A)
