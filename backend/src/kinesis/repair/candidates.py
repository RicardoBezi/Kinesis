"""Candidate repair: contact lock + two-bone IK (docs/ALGORITHMS.md §3), pure numpy.

Write-back to pose-local quaternions uses the extracted data directly. For every chain bone,
the world rotation of its *parent frame* (``parent_world @ inv(rest_parent) @ rest``) is
``F = W_rot @ R(q)^T``, where ``W`` is the bone's world matrix and ``q`` its pose quaternion.
For the thigh this frame is unchanged by the repair (the pelvis is never touched). For the
shin and foot, the rotation from the parent bone's world frame to the child's parent frame
is a constant of the rig, ``C = W_parent_rot^T @ F``, so ``F' = W'_parent_rot @ C``.
This equals step 7.7's ``(parent_world @ rest_offset)^-1`` without assuming connected joints.
The only assumption is that pose scale is 1 on the chain bones.

Segment lengths are measured from the extracted joint positions (hip -> knee -> ankle), so
non-connected offsets are handled exactly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from kinesis.analysis.detection import ContactPlane, project_to_plane
from kinesis.analysis.extraction import ExtractedMotion, foot_and_toe
from kinesis.analysis.kinematics import (
    Array,
    axis_angle_mat3,
    hemisphere_continuous,
    mat3_to_quat,
    quat_to_mat3,
    rotation_between,
    solve_two_bone,
)
from kinesis.schemas.plan import AnchorMode, CandidateParameters
from kinesis.schemas.worker import BoneKeys


@dataclass(frozen=True)
class LegChain:
    thigh: str
    shin: str
    foot: str
    toe: str | None = None

    @property
    def keyable(self) -> tuple[str, str, str]:
        return (self.thigh, self.shin, self.foot)


@dataclass(frozen=True)
class RepairInput:
    """Everything the repair needs, over the context window (index ``i`` = frame_start + i)."""

    frame_start: int
    fps: float
    chain: LegChain
    world: dict[str, Array]  # (T, 4, 4) for thigh, shin, foot
    local: dict[str, Array]  # (T, 4) pose quaternions for thigh, shin, foot
    ball: Array  # (T, 3) ball of the foot (toe head, or foot tail)
    plane: ContactPlane
    floor_height: float
    interval: tuple[int, int]  # worst planted interval [a, b], scene frames

    @property
    def frame_count(self) -> int:
        return int(self.world[self.chain.foot].shape[0])

    @property
    def ankle(self) -> Array:
        out: Array = self.world[self.chain.foot][:, :3, 3]
        return out


@dataclass(frozen=True)
class CandidateResult:
    keys: dict[str, tuple[tuple[int, ...], Array]]  # bone -> (frames, (n, 4) quaternions)
    weights: Array  # w[t] over the context window
    envelope: Array
    target_ankle: Array  # p'[t] (after the height clamp)
    effector: Array  # where IK actually put the ankle (differs only when clamped)
    foot_rotation: Array  # (T, 3, 3) target foot world rotation R'_foot (step 6)
    ik_unreachable_frames: int
    window: tuple[int, int] | None  # first and last keyed frame

    def bone_keys(self) -> tuple[BoneKeys, ...]:
        return tuple(
            BoneKeys(
                bone=bone,
                frames=frames,
                rotation_quaternion=tuple(
                    (float(q[0]), float(q[1]), float(q[2]), float(q[3])) for q in quats
                ),
            )
            for bone, (frames, quats) in self.keys.items()
            if frames
        )


# ---------------------------------------------------------------- input assembly


def repair_input(
    motion: ExtractedMotion,
    chain: LegChain,
    interval: tuple[int, int],
    floor_height: float,
    plane: ContactPlane | None = None,
) -> RepairInput:
    _, ball = foot_and_toe(motion, chain.foot, chain.toe)
    return RepairInput(
        frame_start=motion.context_range[0],
        fps=motion.fps,
        chain=chain,
        world={b: motion.chain_world[b] for b in chain.keyable},
        local={b: motion.chain_local[b] for b in chain.keyable},
        ball=ball,
        plane=plane or ContactPlane(),
        floor_height=floor_height,
        interval=interval,
    )


# ---------------------------------------------------------------- steps 1-6


def smoothstep(x: Array) -> Array:
    c = np.clip(x, 0.0, 1.0)
    out: Array = c * c * (3 - 2 * c)
    return out


def envelope(count: int, frame_start: int, interval: tuple[int, int], blend: int) -> Array:
    """Step 2, already clipped to the context window."""
    a, b = interval[0] - frame_start, interval[1] - frame_start
    t = np.arange(count, dtype=np.float64)
    env = np.zeros(count)
    env[(t >= a) & (t <= b)] = 1.0
    if blend > 0:
        before = (t >= a - blend) & (t < a)
        after = (t > b) & (t <= b + blend)
        env[before] = smoothstep((t[before] - (a - blend)) / blend)
        env[after] = smoothstep(((b + blend) - t[after]) / blend)
    return env


def moving_average(values: Array, window: int) -> Array:
    """Centered moving average along axis 0, edge-padded (step 4)."""
    if window <= 1:
        return np.array(values, dtype=np.float64)
    half = window // 2
    padded = np.concatenate(
        [np.repeat(values[:1], half, 0), values, np.repeat(values[-1:], half, 0)]
    )
    kernel = np.ones(window) / window
    out: Array = np.stack(
        [np.convolve(padded[:, j], kernel, mode="valid") for j in range(values.shape[1])], axis=1
    )
    return out


def _yaw_delta(rot_t: Array, rot_ref: Array, normal: Array) -> float:
    """Signed angle about ``normal`` from the foot's heading at ``t`` to the reference heading."""

    def heading(r: Array) -> Array | None:
        h = r[:, 1] - float(np.dot(r[:, 1], normal)) * normal
        n = float(np.linalg.norm(h))
        return None if n < 1e-9 else h / n

    h_t, h_ref = heading(rot_t), heading(rot_ref)
    if h_t is None or h_ref is None:
        return 0.0
    return math.atan2(float(np.dot(np.cross(h_t, h_ref), normal)), float(np.dot(h_t, h_ref)))


# ---------------------------------------------------------------- entry point


def generate_candidate(inp: RepairInput, params: CandidateParameters) -> CandidateResult:
    """ALGORITHMS §3.3. Pure: inputs are never mutated."""
    count = inp.frame_count
    a, b = inp.interval[0] - inp.frame_start, inp.interval[1] - inp.frame_start
    if not 0 <= a <= b < count:
        raise ValueError("interval lies outside the context window")
    origin, normal = inp.plane.arrays()
    p = inp.ankle
    u = project_to_plane(p, inp.plane)

    # 1. anchor
    anchor = u[a] if params.anchor_mode is AnchorMode.ONSET else u[a : b + 1].mean(axis=0)
    # 2-3. envelope and weights
    env = envelope(count, inp.frame_start, inp.interval, params.blend_frames)
    w = params.lock_strength * env
    active = env > 0
    # 4. horizontal correction, smoothing, tolerance snap
    delta = w[:, None] * (anchor - u)
    if params.smoothing_window > 0:
        delta = moving_average(delta, params.smoothing_window) * active[:, None]
    tol = params.tolerance_cm / 100
    for t in range(a, b + 1):
        if np.linalg.norm(u[t] + delta[t] - anchor) < tol:
            delta[t] = anchor - u[t]

    # 6. target foot rotation (before 5, because the clamp carries the toe with the foot)
    foot_rot = inp.world[inp.chain.foot][:, :3, :3]
    new_foot_rot = foot_rot.copy()
    if params.lock_yaw:
        for t in np.flatnonzero(active).tolist():
            angle = w[t] * _yaw_delta(foot_rot[t], foot_rot[a], normal)
            new_foot_rot[t] = axis_angle_mat3(normal, angle) @ foot_rot[t]

    # 5. target ankle, with the toe carried rigidly by the (possibly re-yawed) foot
    target = p + delta
    if params.height_clamp:
        for t in np.flatnonzero(active).tolist():
            toe_offset = new_foot_rot[t] @ foot_rot[t].T @ (inp.ball[t] - p[t])
            h = min(
                float(np.dot(target[t] - origin, normal)),
                float(np.dot(target[t] + toe_offset - origin, normal)),
            )
            if h < inp.floor_height:
                target[t] = target[t] + (inp.floor_height - h) * normal

    # 7. two-bone IK and local write-back
    thigh_w, shin_w = inp.world[inp.chain.thigh], inp.world[inp.chain.shin]
    q = {bone: quat_to_mat3(inp.local[bone]) for bone in inp.chain.keyable}
    hip = thigh_w[:, :3, 3]
    knee = shin_w[:, :3, 3]
    upper = float(np.linalg.norm(knee[a] - hip[a]))
    lower = float(np.linalg.norm(p[a] - knee[a]))

    frames: list[int] = []
    quats: dict[str, list[Array]] = {bone: [] for bone in inp.chain.keyable}
    effector = p.copy()
    unreachable = 0
    for t in np.flatnonzero(active).tolist():
        sol = solve_two_bone(hip[t], target[t], knee[t], upper, lower)
        unreachable += int(sol.unreachable)
        effector[t] = sol.effector
        r_thigh = thigh_w[t, :3, :3]
        r_shin = shin_w[t, :3, :3]
        new_thigh = rotation_between(knee[t] - hip[t], sol.knee - hip[t]) @ r_thigh
        new_shin = rotation_between(p[t] - knee[t], sol.effector - sol.knee) @ r_shin

        f_thigh = r_thigh @ q[inp.chain.thigh][t].T  # parent frame: unchanged
        c_shin = r_thigh.T @ (r_shin @ q[inp.chain.shin][t].T)
        c_foot = r_shin.T @ (foot_rot[t] @ q[inp.chain.foot][t].T)
        locals_ = {
            inp.chain.thigh: f_thigh.T @ new_thigh,
            inp.chain.shin: (new_thigh @ c_shin).T @ new_shin,
            inp.chain.foot: (new_shin @ c_foot).T @ new_foot_rot[t],
        }
        frames.append(inp.frame_start + int(t))
        for bone, m in locals_.items():
            quats[bone].append(mat3_to_quat(m))

    keys: dict[str, tuple[tuple[int, ...], Array]] = {}
    for bone in inp.chain.keyable:
        if not frames:
            keys[bone] = ((), np.zeros((0, 4)))
            continue
        arr = np.array(quats[bone])
        first = int(np.flatnonzero(active)[0])
        if float(np.dot(arr[0], inp.local[bone][first])) < 0:  # match the original's hemisphere
            arr[0] = -arr[0]
        keys[bone] = (tuple(frames), hemisphere_continuous(arr))
    return CandidateResult(
        keys=keys,
        weights=w,
        envelope=env,
        target_ankle=target,
        effector=effector,
        foot_rotation=new_foot_rot,
        ik_unreachable_frames=unreachable,
        window=(frames[0], frames[-1]) if frames else None,
    )


__all__ = [
    "CandidateResult",
    "LegChain",
    "RepairInput",
    "envelope",
    "generate_candidate",
    "moving_average",
    "repair_input",
    "smoothstep",
]
