"""Minimal async client for Nebius Token Factory's OpenAI-compatible API (ADR 0009).

Request and response shapes are explicit. Transport failures are classified into
``kinesis.errors`` provider errors so retries and breakers can act on them:

| failure | error | retryable | breaker |
|---|---|---|---|
| timeout | ``ProviderTimeout`` | yes | counts |
| HTTP 429 | ``ProviderRateLimited`` (``Retry-After`` honoured) | yes | counts |
| HTTP 5xx, network error, non-JSON body | ``ProviderServerError`` | yes | counts |
| other HTTP 4xx | ``ProviderClientError`` | no | neutral |

The API key is sent only in the ``Authorization`` header and never appears in messages.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from kinesis.errors import (
    ProviderClientError,
    ProviderRateLimited,
    ProviderServerError,
    ProviderTimeout,
)
from kinesis.schemas import TokenUsage

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1/"
_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove one surrounding markdown code fence, if present."""
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text.strip()


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str | None  # fences stripped; None when the response has no choices or content
    model: str
    usage: TokenUsage
    reasoning_tokens: int
    latency_ms: int
    finish_reason: str | None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    input_modalities: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()
    price_in_per_m: float | None = None  # USD per 1M prompt tokens
    price_out_per_m: float | None = None  # USD per 1M completion tokens
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def accepts_images(self) -> bool:
        return "image" in self.input_modalities


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_model(entry: dict[str, Any]) -> ModelInfo:
    """Read one ``/models?verbose=true`` entry. Unknown shapes degrade to an id-only record."""
    modality = str((entry.get("architecture") or {}).get("modality") or "")
    inputs = modality.split("->", 1)[0] if "->" in modality else ""
    features = entry.get("supported_features") or []
    pricing = entry.get("pricing") or {}
    return ModelInfo(
        id=str(entry.get("id", "")),
        input_modalities=frozenset(p for p in inputs.split("+") if p),
        features=frozenset(str(f) for f in features if isinstance(f, str)),
        price_in_per_m=_float(pricing.get("prompt")),
        price_out_per_m=_float(pricing.get("completion")),
        raw=entry,
    )


class TokenFactoryClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Token Factory needs NEBIUS_API_KEY")
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "kinesis/0.1"},
            timeout=httpx.Timeout(timeout_s, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kw)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout(f"{method} {path}: timed out ({type(exc).__name__})") from None
        except httpx.TransportError as exc:
            raise ProviderServerError(f"{method} {path}: {type(exc).__name__}") from None
        status = response.status_code
        if status == 429:
            raise ProviderRateLimited(
                f"{method} {path}: HTTP 429 ({_error_message(response)})",
                retry_after_s=_retry_after(response),
            )
        if status >= 500:
            raise ProviderServerError(
                f"{method} {path}: HTTP {status} ({_error_message(response)})"
            )
        if status >= 400:
            raise ProviderClientError(
                f"{method} {path}: HTTP {status} ({_error_message(response)})"
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderServerError(f"{method} {path}: response is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderServerError(f"{method} {path}: unexpected response shape")
        return data

    async def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if response_format is not None:
            body["response_format"] = response_format
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        started = time.monotonic()
        data = await self._request("POST", "chat/completions", json=body)
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        raw = message.get("content")
        content = strip_fences(raw) if isinstance(raw, str) and raw.strip() else None
        return ChatResult(
            content=content,
            model=str(data.get("model") or model),
            usage=TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
            reasoning_tokens=int(details.get("reasoning_tokens") or 0),
            latency_ms=latency_ms,
            finish_reason=choices[0].get("finish_reason") if choices else None,
        )

    async def list_models(self, *, verbose: bool = True) -> list[ModelInfo]:
        params = {"verbose": "true"} if verbose else None
        data = await self._request("GET", "models", params=params)
        return [parse_model(m) for m in data.get("data") or [] if isinstance(m, dict)]


def _error_message(response: httpx.Response) -> str:
    try:
        err = response.json().get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or "error")[:200]
        if isinstance(err, str):
            return err[:200]
    except ValueError:
        pass
    return response.reason_phrase or "error"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), 120.0))
    except ValueError:
        return None


__all__ = [
    "DEFAULT_BASE_URL",
    "ChatResult",
    "ModelInfo",
    "TokenFactoryClient",
    "parse_model",
    "strip_fences",
]
