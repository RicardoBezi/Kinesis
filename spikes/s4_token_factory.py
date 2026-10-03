"""Spike S4: check Nebius Token Factory capabilities before Phase 4 depends on them (ADR 0009).

    uv run python spikes/s4_token_factory.py --list          # GET /models (task models)
    uv run python spikes/s4_token_factory.py --probe-json    # structured output (planner)
    uv run python spikes/s4_token_factory.py --probe-vision  # image input (vision model)

It needs NEBIUS_API_KEY. The base URL comes from KINESIS_TF_BASE_URL. The default below is
a GUESS and must be confirmed against the current Nebius docs: recording the confirmed URL
is part of this spike.

Questions it answers (record the answers in spikes/README.md):
  Q1 the base URL and auth header, plus whether /models lists Nemotron 3 Super / Nano Omni ids;
  Q2 whether `response_format: {"type": "json_schema", ...}` is honoured, or only json_object;
  Q3 whether image input works as an `image_url` data URL, and how many images per request;
  Q4 whether `usage` (prompt/completion tokens) is returned;
  Q5 latency per call, as a rough figure.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import time
import zlib
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"  # UNVERIFIED: confirm in S4


def client() -> httpx.Client:
    key = os.environ.get("NEBIUS_API_KEY")
    if not key:
        sys.exit("NEBIUS_API_KEY is not set")
    base = os.environ.get("KINESIS_TF_BASE_URL") or DEFAULT_BASE_URL
    return httpx.Client(
        base_url=base.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {key}"},
        timeout=60,
    )


def list_models(c: httpx.Client) -> None:
    r = c.get("models")
    print(f"GET {r.request.url} -> {r.status_code}")
    r.raise_for_status()
    ids = sorted(m["id"] for m in r.json().get("data", []))
    for i in ids:
        flag = "  <-- nemotron" if "nemotron" in i.lower() else ""
        print(f"  {i}{flag}")


def chat(c: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    r = c.post("chat/completions", json=body)
    ms = int((time.perf_counter() - t0) * 1000)
    print(f"POST chat/completions model={body['model']} -> {r.status_code} in {ms} ms")
    if r.status_code >= 400:
        print(r.text[:2000])
        r.raise_for_status()
    data: dict[str, Any] = r.json()
    print("usage:", data.get("usage"))
    return data


def probe_json(c: httpx.Client) -> None:
    model = os.environ.get("KINESIS_PLANNER_MODEL") or sys.exit("set KINESIS_PLANNER_MODEL")
    schema = {
        "type": "object",
        "properties": {
            "lock_strength": {"type": "number", "minimum": 0, "maximum": 1},
            "explanation": {"type": "string"},
        },
        "required": ["lock_strength", "explanation"],
        "additionalProperties": False,
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Reply with JSON only."},
            {"role": "user", "content": "A foot slides 10 cm while planted. Pick lock_strength."},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "probe", "schema": schema, "strict": True},
        },
        "temperature": 0,
    }
    data = chat(c, body)
    content = data["choices"][0]["message"]["content"]
    print("content:", content)
    try:
        print("parsed OK:", json.loads(content))
    except json.JSONDecodeError:
        print("NOT valid JSON: json_schema may be unsupported, so fall back to json_object")


def tiny_png(w: int = 64, h: int = 64) -> bytes:
    """Return a solid grey PNG, built with the standard library only (no Pillow)."""
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * w for _ in range(h))

    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    sig = b"\x89PNG\r\n\x1a\n"
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def probe_vision(c: httpx.Client, n_images: int) -> None:
    model = os.environ.get("KINESIS_VISION_MODEL") or sys.exit("set KINESIS_VISION_MODEL")
    url = "data:image/png;base64," + base64.b64encode(tiny_png()).decode()
    content: list[dict[str, Any]] = [
        {"type": "text", "text": "How many images do you see? Reply with JSON {'n': <int>}."}
    ]
    content += [{"type": "image_url", "image_url": {"url": url}} for _ in range(n_images)]
    data = chat(c, {"model": model, "messages": [{"role": "user", "content": content}]})
    print("content:", data["choices"][0]["message"]["content"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--probe-json", action="store_true")
    ap.add_argument("--probe-vision", action="store_true")
    ap.add_argument("--images", type=int, default=4)
    args = ap.parse_args()
    with client() as c:
        if args.list:
            list_models(c)
        if args.probe_json:
            probe_json(c)
        if args.probe_vision:
            probe_vision(c, args.images)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
