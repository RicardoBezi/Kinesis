"""Settings from .env: placeholders copied from .env.example must not break start-up."""

from __future__ import annotations

from pathlib import Path

import pytest

from kinesis.settings import Settings


def test_env_example_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    for key in [k for k in __import__("os").environ if k.startswith(("KINESIS_", "NEBIUS_"))]:
        monkeypatch.delenv(key, raising=False)
    s = Settings()
    assert s.nebius_price_per_hour_usd is None  # `NEBIUS_PRICE_PER_HOUR_USD=` is unset
    assert s.nebius_auth_pem is None
    assert s.nebius_budget_usd == 5
    assert s.kinesis_vision_model == "zai-org/GLM-5.3-Flash"
