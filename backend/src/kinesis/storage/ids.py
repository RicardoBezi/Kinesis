"""Sortable, URL-safe identifiers (ARCHITECTURE §4): ``<prefix>_<time><random>``, lowercase.

The time part is 10 Crockford base-32 characters of milliseconds since the epoch, so ids sort
by creation time; 16 random characters follow. Every id matches ``schemas.common.Identifier``.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def new_id(prefix: str, *, now_ms: int | None = None) -> str:
    if not prefix.isalpha() or not prefix.islower() or len(prefix) > 8:
        raise ValueError(f"invalid id prefix {prefix!r}")
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    return f"{prefix}_{_encode(ms, 10)}{_encode(secrets.randbits(80), 16)}"


__all__ = ["new_id"]
