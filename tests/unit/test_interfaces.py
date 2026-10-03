"""Interface-level guarantees: cache keys, job-dir confinement, NullProvider, import purity."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kinesis.jobs.runner import assert_inside
from kinesis.orchestration.cache import cache_key
from kinesis.providers.base import ModelProvider, PlanRequest, ResultStatus
from kinesis.providers.null import NullProvider
from kinesis.schemas import (
    AnimationSelection,
    DefectReport,
    PlanSource,
    Severity,
    SkeletalScope,
)

PKG = Path(__file__).resolve().parents[2] / "backend" / "src" / "kinesis"

# ------------------------------------------------------------------ cache keys


def test_cache_key_is_deterministic_and_order_independent() -> None:
    a = cache_key("extract", {"blend": "abc", "bones": ["foot.L"]}, 3)
    b = cache_key("extract", {"bones": ["foot.L"], "blend": "abc"}, 3)
    assert a == b
    assert a.startswith("kinesis:extract:")
    assert a != cache_key("extract", {"blend": "abd", "bones": ["foot.L"]}, 3)
    assert a != cache_key("analysis", {"blend": "abc", "bones": ["foot.L"]}, 3)


def test_cache_namespace_validated() -> None:
    with pytest.raises(ValueError, match="namespace"):
        cache_key("bad ns:*", 1)


# ------------------------------------------------------------------ job dir confinement


@pytest.mark.parametrize(
    "rel", ["../x", "work/../../x", "/etc/passwd", "\\\\server\\share", "", "a/../../b"]
)
def test_assert_inside_rejects_escapes(tmp_path: Path, rel: str) -> None:
    with pytest.raises(ValueError, match=r"unsafe|escapes"):
        assert_inside(tmp_path, rel)


def test_assert_inside_accepts_job_paths(tmp_path: Path) -> None:
    p = assert_inside(tmp_path, "work/extract_scope.spec.json")
    assert p.is_relative_to(tmp_path.resolve())


# ------------------------------------------------------------------ null provider


async def test_null_provider_returns_fallback_plan(
    selection: AnimationSelection, scope: SkeletalScope
) -> None:
    provider = NullProvider()
    assert isinstance(provider, ModelProvider)
    defect = DefectReport(intervals=(), severity=Severity.NONE, summary="", algorithm_version="v")
    result = await provider.plan_repair(PlanRequest(selection, scope, defect, (1, 120)))
    assert result.status is ResultStatus.UNAVAILABLE
    assert result.value is not None
    assert result.value.plan_source is PlanSource.FALLBACK
    assert (await provider.health_check()).ok


# ------------------------------------------------------------------ import purity (ADR 0003)

PURE_PACKAGES = ("analysis", "repair", "evaluation", "schemas")
FORBIDDEN = {"fastapi", "starlette", "redis", "httpx", "httpx2", "bpy", "mathutils", "uvicorn"}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("package", PURE_PACKAGES)
def test_pure_packages_import_no_io_frameworks(package: str) -> None:
    offenders = {
        str(f.relative_to(PKG)): sorted(_imported_roots(f) & FORBIDDEN)
        for f in (PKG / package).rglob("*.py")
        if _imported_roots(f) & FORBIDDEN
    }
    assert not offenders, offenders


def test_no_unsafe_deserialization_anywhere() -> None:
    banned = {"pickle", "marshal", "shelve", "dill"}
    for f in PKG.rglob("*.py"):
        assert not (_imported_roots(f) & banned), f
        src = f.read_text(encoding="utf-8")
        assert "eval(" not in src.replace("_eval(", ""), f
        assert "exec(" not in src, f
        assert "shell=True" not in src, f
