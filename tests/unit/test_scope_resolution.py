"""Temporal and skeletal cropping (ALGORITHMS §1)."""

from __future__ import annotations

import pytest

from kinesis.analysis.scope import resolve_skeletal, resolve_temporal
from kinesis.errors import SelectionInvalid, is_retryable
from kinesis.schemas import ErrorCode, SkeletalScope, TemporalScope
from kinesis.testing.synthetic import RIG_BONES

PARENTS = {name: parent for name, parent, *_ in RIG_BONES}


@pytest.mark.parametrize(
    ("start", "end", "before", "after", "expected"),
    [
        (45, 90, 10, 10, (35, 100)),
        (1, 10, 10, 10, (1, 20)),  # clipped at the scene start
        (110, 120, 10, 10, (100, 120)),  # clipped at the scene end
        (50, 50, 0, 0, (50, 50)),
    ],
)
def test_temporal_crop(
    start: int, end: int, before: int, after: int, expected: tuple[int, int]
) -> None:
    t = TemporalScope(frame_start=start, frame_end=end, context_before=before, context_after=after)
    assert resolve_temporal(t, (1, 120)) == expected


@pytest.mark.parametrize(("start", "end"), [(0, 10), (100, 121)])
def test_temporal_range_outside_scene(start: int, end: int) -> None:
    with pytest.raises(SelectionInvalid) as err:
        resolve_temporal(TemporalScope(frame_start=start, frame_end=end), (1, 120))
    assert err.value.code is ErrorCode.INVALID_FRAME_RANGE
    assert not is_retryable(err.value)


def test_skeletal_crop_on_fixture_rig(scope: SkeletalScope) -> None:
    assert resolve_skeletal("Rig", ("foot.L",), PARENTS) == scope


def test_skeletal_crop_both_feet() -> None:
    s = resolve_skeletal("Rig", ("foot.L", "foot.R"), PARENTS)
    assert set(s.keyable_bones) == {"thigh.L", "shin.L", "foot.L", "thigh.R", "shin.R", "foot.R"}
    assert s.context_bones == ("pelvis", "root")


def test_unknown_bone() -> None:
    with pytest.raises(SelectionInvalid) as err:
        resolve_skeletal("Rig", ("foot.X",), PARENTS)
    assert err.value.code is ErrorCode.BONE_NOT_FOUND


@pytest.mark.parametrize("target", ["root", "pelvis"])
def test_bone_without_leg_chain_is_unsupported(target: str) -> None:
    with pytest.raises(SelectionInvalid) as err:
        resolve_skeletal("Rig", (target,), PARENTS)
    assert err.value.code is ErrorCode.UNSUPPORTED_RIG


def test_nested_targets_are_unsupported() -> None:
    with pytest.raises(SelectionInvalid, match="ancestors"):
        resolve_skeletal("Rig", ("foot.L", "shin.L"), PARENTS)


def test_cyclic_hierarchy_is_unsupported() -> None:
    with pytest.raises(SelectionInvalid, match="cycle"):
        resolve_skeletal("Rig", ("c",), {"a": "c", "b": "a", "c": "b"})


def test_toe_leaf_bones_join_the_chain() -> None:
    """Quaternius UAL / UE-style legs end in ball -> ball_leaf: the leaf follows the foot, so it
    belongs to the chain (not keyed, and not counted as collateral motion)."""
    parents = {
        "root": None,
        "pelvis": "root",
        "thigh_l": "pelvis",
        "calf_l": "thigh_l",
        "foot_l": "calf_l",
        "ball_l": "foot_l",
        "ball_leaf_l": "ball_l",
    }
    s = resolve_skeletal("Armature", ("foot_l",), parents)
    assert s.chain_bones == ("thigh_l", "calf_l", "foot_l", "ball_l", "ball_leaf_l")
    assert s.keyable_bones == ("thigh_l", "calf_l", "foot_l")
    assert s.context_bones == ("pelvis", "root")
