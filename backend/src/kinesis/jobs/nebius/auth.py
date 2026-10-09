"""Nebius service-account authentication (docs.nebius.com/rest-api/authentication).

An authorized key (RSA private PEM, its key id, and the service account id) signs a short
RS256 JWT (``iss = sub = <service account>``, 5 minute lifetime). The JWT is exchanged
(RFC 8693) at ``https://auth.eu.nebius.com/oauth2/token/exchange`` for a bearer token valid
for 12 hours. Tokens are cached and refreshed early; ``invalidate()`` forces a refresh after
a 401.

The private key is read from a file path; it never appears in logs, errors or settings dumps.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from kinesis.errors import KinesisError
from kinesis.schemas.common import ErrorCode

TOKEN_EXCHANGE_URL = "https://auth.eu.nebius.com/oauth2/token/exchange"  # noqa: S105 - a URL
JWT_LIFETIME_S = 300
REFRESH_MARGIN_S = 600


class NebiusAuthError(KinesisError):
    """Credentials are missing or rejected. Not retryable: a retry cannot fix a bad key."""

    code = ErrorCode.WORKER_CRASHED


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class ServiceAccountKey:
    service_account_id: str
    key_id: str
    private_key_path: Path

    def sign_jwt(self, now: float) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        try:
            key = serialization.load_pem_private_key(self.private_key_path.read_bytes(), None)
        except (OSError, ValueError) as exc:
            raise NebiusAuthError(
                f"cannot load the authorized key PEM: {type(exc).__name__}"
            ) from None
        if not isinstance(key, rsa.RSAPrivateKey):
            raise NebiusAuthError("the authorized key must be an RSA private key")
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key_id}
        claims = {
            "iss": self.service_account_id,
            "sub": self.service_account_id,
            "iat": int(now),
            "exp": int(now) + JWT_LIFETIME_S,
        }
        signing_input = (
            f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
            f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
        )
        signature = key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input}.{_b64url(signature)}"


class NebiusTokenProvider:
    def __init__(
        self,
        key: ServiceAccountKey,
        *,
        exchange_url: str = TOKEN_EXCHANGE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = key
        self._url = exchange_url
        self._http = httpx.AsyncClient(timeout=30, transport=transport)
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        self.exchanges = 0

    async def token(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._expires_at - REFRESH_MARGIN_S:
                await self._exchange()
            assert self._token is not None
            return self._token

    def invalidate(self) -> None:
        self._token = None

    async def _exchange(self) -> None:
        jwt = self._key.sign_jwt(self._clock())
        try:
            response = await self._http.post(
                self._url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "subject_token": jwt,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
                },
            )
        except httpx.HTTPError as exc:
            raise NebiusAuthError(f"token exchange failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise NebiusAuthError(f"token exchange rejected: HTTP {response.status_code}")
        body = response.json()
        self._token = str(body["access_token"])
        self._expires_at = self._clock() + float(body.get("expires_in", 43200))
        self.exchanges += 1

    async def aclose(self) -> None:
        await self._http.aclose()


__all__ = ["TOKEN_EXCHANGE_URL", "NebiusAuthError", "NebiusTokenProvider", "ServiceAccountKey"]
