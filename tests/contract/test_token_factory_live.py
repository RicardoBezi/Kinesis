"""Live Nebius Token Factory tests. They NEVER run by default.

Two gates must both be open: the ``live_nebius`` marker must be selected, and
``KINESIS_LIVE_NEBIUS=1`` must be set (see tests/conftest.py). Run them with
``uv run task live-nebius-test``. They read NEBIUS_API_KEY and the model ids from .env.

Cost per full run is about a cent: one planner call (capped at 4096 tokens, mostly reasoning)
and one vision call with 10 small images.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from kinesis.analysis.detection import detect_foot_slide
from kinesis.api.deps import build_provider
from kinesis.providers.base import EvaluationRequest, PlanRequest, ResultStatus
from kinesis.providers.token_factory import TokenFactoryProvider
from kinesis.schemas import (
    DEFAULT_CANDIDATES,
    AnimationSelection,
    CandidateLabel,
    CandidateMetrics,
    PlanSource,
    RepairCandidate,
    SkeletalScope,
)
from kinesis.settings import ProviderKind, Settings
from kinesis.testing.synthetic import foot_slide_v1

pytestmark = pytest.mark.live_nebius


def report(text: str) -> None:
    """Print model output safely on consoles without UTF-8 (Windows cp1252)."""
    print(text.encode("ascii", "replace").decode("ascii"))


@pytest.fixture
async def provider() -> AsyncIterator[TokenFactoryProvider]:
    settings = Settings().model_copy(update={"kinesis_provider": ProviderKind.TOKEN_FACTORY})
    p = build_provider(settings)
    assert isinstance(p, TokenFactoryProvider)
    yield p
    await p.aclose()


def grey_png(path: Path, shade: int) -> Path:
    w = h = 64
    raw = b"".join(b"\x00" + bytes([shade, shade, shade]) * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


async def test_catalog_has_configured_models_and_prices(provider: TokenFactoryProvider) -> None:
    assert await provider.verify_models() == []
    assert provider.planner_model in provider.prices
    assert provider.vision_model in provider.prices


async def test_planner_returns_valid_plan_for_fixture_defect(
    provider: TokenFactoryProvider, selection: AnimationSelection, scope: SkeletalScope
) -> None:
    await provider.verify_models()  # loads prices for the cost estimate
    scene = foot_slide_v1()
    window = slice(34, 100)
    detection = detect_foot_slide(
        scene.head("foot.L")[window],
        scene.head("toe.L")[window],
        frame_start=35,
        fps=24,
        selection=(45, 90),
        bone="foot.L",
    )
    result = await provider.plan_repair(PlanRequest(selection, scope, detection.report, (1, 120)))
    assert result.status is ResultStatus.OK, result.reasons
    assert result.value is not None
    assert result.value.plan_source is PlanSource.MODEL
    assert result.usage.prompt_tokens > 0
    assert result.usage.completion_tokens > 0
    assert result.cost_usd is not None
    assert result.cost_usd < 0.02
    report(
        f"\nplan: {result.value.explanation}\nusage={result.usage} cost=${result.cost_usd:.5f} "
        f"latency={result.latency_ms} ms"
    )


async def test_vision_model_accepts_frames_and_returns_valid_judgement(
    provider: TokenFactoryProvider, selection: AnimationSelection, tmp_path: Path
) -> None:
    await provider.verify_models()
    original = tuple(grey_png(tmp_path / f"o{i}.png", 90 + i) for i in range(5))
    candidate_frames = tuple(grey_png(tmp_path / f"c{i}.png", 140 + i) for i in range(5))
    metrics = CandidateMetrics(
        planted_displacement_cm_before=10.0,
        planted_displacement_cm_after=0.0,
        slip_reduction_pct=100.0,
        contact_error_cm=0.0,
        penetration_max_cm=0.0,
        jerk_rms_ratio=1.79,
        joint_limit_violations=0,
        target_deviation_rms_cm=5.04,
        collateral_max_cm=0.0,
        outside_window_max_cm=0.0,
        root_deviation_cm=0.0,
        ik_unreachable_frames=0,
    )
    candidate = RepairCandidate(
        candidate_id="0123456789abcdef",
        label=CandidateLabel.A,
        parameters=DEFAULT_CANDIDATES[CandidateLabel.A],
    )
    req = EvaluationRequest(
        selection, candidate, metrics, original, candidate_frames, (45, 55, 65, 75, 85)
    )
    result = await provider.evaluate_candidate(req)
    assert result.status in (ResultStatus.OK, ResultStatus.INVALID)  # grey squares are odd input
    assert result.usage.prompt_tokens > 0
    if result.status is ResultStatus.OK:
        assert result.value is not None
        assert 1 <= result.value.contact_stability <= 5
    report(
        f"\nvision: {result.status} {result.value.notes if result.value else result.reasons} "
        f"usage={result.usage} cost=${result.cost_usd or 0:.5f} latency={result.latency_ms} ms"
    )
