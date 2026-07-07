"""sigv4 scheme: AWS Signature Version 4 request signing (docs/23 § sigv4).

Pure-stdlib signer (hashlib + hmac) — no boto3 dependency. Credentials carry
``access_key_id`` + ``secret_access_key`` (+ optional ``session_token``).
``kind=default`` chain resolution (IAM role, SSO profile, instance metadata)
is deferred until a consumer needs it; env-sourced static keys cover 2a.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, ConfigDict

from foundry.core.connection import ResolvedConnectionCredentials


class SigV4Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str
    region: str


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        pairs.append((quote(key, safe="-_.~"), quote(value, safe="-_.~")))
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def sign_request(
    config: SigV4Config,
    credentials: ResolvedConnectionCredentials,
    *,
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    now: datetime | None = None,
) -> dict[str, str]:
    """Return the request headers with SigV4 Authorization applied."""
    access_key = credentials.require("access_key_id").reveal()
    secret_key = credentials.require("secret_access_key").reveal()
    session_token = (
        credentials.fields["session_token"].reveal()
        if "session_token" in credentials.fields
        else None
    )

    parts = urlsplit(url)
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    datestamp = timestamp[:8]

    out_headers = dict(headers or {})
    out_headers["host"] = parts.netloc
    out_headers["x-amz-date"] = timestamp
    if session_token:
        out_headers["x-amz-security-token"] = session_token

    payload_hash = hashlib.sha256(body).hexdigest()
    out_headers.setdefault("x-amz-content-sha256", payload_hash)

    sorted_header_items = sorted(
        (k.lower().strip(), " ".join(v.split())) for k, v in out_headers.items()
    )
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted_header_items)
    signed_headers = ";".join(k for k, _ in sorted_header_items)

    canonical_request = "\n".join(
        (
            method.upper(),
            quote(parts.path or "/", safe="/-_.~"),
            _canonical_query(parts.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        )
    )
    scope = f"{datestamp}/{config.region}/{config.service}/aws4_request"
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            timestamp,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        )
    )
    key = _hmac_sha256(f"AWS4{secret_key}".encode(), datestamp)
    key = _hmac_sha256(key, config.region)
    key = _hmac_sha256(key, config.service)
    key = _hmac_sha256(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    out_headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out_headers


__all__ = ["SigV4Config", "sign_request"]
