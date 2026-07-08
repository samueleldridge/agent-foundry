"""Auth scheme helpers, token cache, redactor (docs/23 § Auth schemes)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from foundry.auth.redactor import looks_secret, redact_config
from foundry.auth.schemes import api_key, basic_auth, jwt_bearer, mtls, sigv4
from foundry.auth.schemes.custom import validate_custom_auth
from foundry.auth.schemes.oauth2_client_creds import (
    OAuth2ClientCredentialsConfig,
    fetch_access_token,
)
from foundry.auth.token_cache import TokenCache
from foundry.core.connection import (
    AuthScheme,
    ResolvedConnectionCredentials,
    SecretValue,
)
from foundry.core.errors import (
    ConnectionAuthError,
    ConnectionConfigError,
)


def _creds(scheme: AuthScheme, **fields: str) -> ResolvedConnectionCredentials:
    return ResolvedConnectionCredentials(
        scheme=scheme, fields={k: SecretValue(v) for k, v in fields.items()}
    )


# --- SecretValue / credentials non-leak ----------------------------------------


@pytest.mark.unit
def test_secret_value_never_prints_its_value() -> None:
    secret = SecretValue("hunter2-super-secret")
    assert "hunter2" not in str(secret)
    assert "hunter2" not in repr(secret)
    assert "hunter2" not in f"formatted: {secret}"
    assert secret.reveal() == "hunter2-super-secret"


@pytest.mark.unit
def test_credentials_repr_redacts_but_names_fields() -> None:
    creds = _creds(AuthScheme.BASIC_AUTH, username="u", password="p-secret")
    text = repr(creds)
    assert "p-secret" not in text
    assert "password" in text  # field NAMES are safe and useful


@pytest.mark.unit
def test_credentials_require_missing_field_is_structured() -> None:
    creds = _creds(AuthScheme.API_KEY)
    with pytest.raises(ConnectionAuthError) as excinfo:
        creds.require("api_key")
    assert excinfo.value.context["missing_field"] == "api_key"


# --- api_key / basic_auth --------------------------------------------------------


@pytest.mark.unit
def test_api_key_header_injection_with_template() -> None:
    headers = api_key.build_headers(
        api_key.APIKeyConfig(header_name="X-Api-Key", value_template="{api_key}"),
        _creds(AuthScheme.API_KEY, api_key="k-123"),
    )
    assert headers == {"X-Api-Key": "k-123"}
    default = api_key.build_headers(
        api_key.APIKeyConfig(), _creds(AuthScheme.API_KEY, api_key="k-123")
    )
    assert default == {"Authorization": "Bearer k-123"}


@pytest.mark.unit
def test_basic_auth_header_is_base64_user_pass() -> None:
    headers = basic_auth.build_headers(
        basic_auth.BasicAuthConfig(),
        _creds(AuthScheme.BASIC_AUTH, username="alice", password="s3cret"),
    )
    expected = base64.b64encode(b"alice:s3cret").decode()
    assert headers == {"Authorization": f"Basic {expected}"}


# --- oauth2 client credentials + token cache -------------------------------------


@pytest.mark.unit
async def test_oauth2_client_creds_fetches_and_caches_token() -> None:
    token_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_requests
        token_requests += 1
        body = request.content.decode()
        assert "grant_type=client_credentials" in body
        assert "client_id=cid" in body
        return httpx.Response(
            200, json={"access_token": "tok-1", "expires_in": 3600}
        )

    config = OAuth2ClientCredentialsConfig(token_url="https://auth.test/token")
    creds = _creds(
        AuthScheme.OAUTH2_CLIENT_CREDENTIALS, client_id="cid", client_secret="cs"
    )
    cache = TokenCache()
    transport = httpx.MockTransport(handler)
    first = await fetch_access_token(
        config, creds, cache, cache_key="k", transport=transport
    )
    second = await fetch_access_token(
        config, creds, cache, cache_key="k", transport=transport
    )
    assert first.reveal() == second.reveal() == "tok-1"
    assert token_requests == 1  # cached


@pytest.mark.unit
async def test_oauth2_token_endpoint_error_is_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client"})

    config = OAuth2ClientCredentialsConfig(token_url="https://auth.test/token")
    creds = _creds(
        AuthScheme.OAUTH2_CLIENT_CREDENTIALS, client_id="cid", client_secret="cs"
    )
    with pytest.raises(ConnectionAuthError):
        await fetch_access_token(
            config, creds, TokenCache(), cache_key="k",
            transport=httpx.MockTransport(handler),
        )


@pytest.mark.unit
async def test_token_cache_refetches_inside_early_refresh_window() -> None:
    cache = TokenCache()
    fetches = 0

    async def fetch() -> tuple[str, float | None]:
        nonlocal fetches
        fetches += 1
        return f"tok-{fetches}", time.time() + 30  # expires in 30s

    # buffer 60s > 30s ttl → every call refetches
    await cache.get_or_fetch("k", fetch, early_refresh_buffer_s=60)
    await cache.get_or_fetch("k", fetch, early_refresh_buffer_s=60)
    assert fetches == 2
    # generous ttl → cached
    async def fetch_long() -> tuple[str, float | None]:
        nonlocal fetches
        fetches += 1
        return "tok-long", time.time() + 3600

    await cache.get_or_fetch("k2", fetch_long, early_refresh_buffer_s=60)
    await cache.get_or_fetch("k2", fetch_long, early_refresh_buffer_s=60)
    assert fetches == 3
    cache.evict("k2")
    await cache.get_or_fetch("k2", fetch_long, early_refresh_buffer_s=60)
    assert fetches == 4


# --- jwt_bearer -------------------------------------------------------------------


@pytest.mark.unit
def test_jwt_bearer_hs256_assertion_signs_and_carries_claims() -> None:
    config = jwt_bearer.JWTBearerConfig(
        token_url="https://auth.test/token",
        issuer="iss-1",
        audience="aud-1",
        subject="sub-1",
        scopes=["read"],
        expiry_s=300,
    )
    creds = _creds(AuthScheme.JWT_BEARER, private_key="hs-secret")
    token = jwt_bearer.build_assertion(config, creds, now=1_700_000_000)
    header_b64, claims_b64, signature_b64 = token.split(".")

    def _unb64(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    claims = json.loads(_unb64(claims_b64))
    assert claims["iss"] == "iss-1" and claims["aud"] == "aud-1"
    assert claims["sub"] == "sub-1" and claims["scope"] == "read"
    assert claims["exp"] - claims["iat"] == 300
    expected_sig = hmac.new(
        b"hs-secret", f"{header_b64}.{claims_b64}".encode(), hashlib.sha256
    ).digest()
    assert _unb64(signature_b64) == expected_sig


@pytest.mark.unit
def test_jwt_bearer_rs256_names_missing_dependency() -> None:
    config = jwt_bearer.JWTBearerConfig(
        token_url="https://auth.test/token",
        issuer="i", audience="a", algorithm="RS256",
    )
    with pytest.raises(ConnectionConfigError) as excinfo:
        jwt_bearer.build_assertion(
            config, _creds(AuthScheme.JWT_BEARER, private_key="pem")
        )
    assert "cryptography" in str(excinfo.value)


# --- sigv4 --------------------------------------------------------------------------


@pytest.mark.unit
def test_sigv4_signature_is_deterministic_and_well_formed() -> None:
    config = sigv4.SigV4Config(service="s3", region="us-east-1")
    creds = _creds(
        AuthScheme.SIGV4,
        access_key_id="AKIDEXAMPLE",
        secret_access_key="wJalrXUtnFEMI",
    )
    now = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)
    headers_a = sigv4.sign_request(
        config, creds, method="GET",
        url="https://examplebucket.s3.amazonaws.com/key?b=2&a=1", now=now,
    )
    headers_b = sigv4.sign_request(
        config, creds, method="GET",
        url="https://examplebucket.s3.amazonaws.com/key?a=1&b=2", now=now,
    )
    auth = headers_a["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20260708/")
    assert "us-east-1/s3/aws4_request" in auth
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    # query-param canonicalisation: order must not change the signature
    assert headers_a["Authorization"] == headers_b["Authorization"]
    assert "wJalrXUtnFEMI" not in json.dumps(headers_a)  # secret never in output


@pytest.mark.unit
def test_sigv4_session_token_header_included_when_present() -> None:
    config = sigv4.SigV4Config(service="bedrock", region="us-west-2")
    creds = _creds(
        AuthScheme.SIGV4,
        access_key_id="AKID",
        secret_access_key="sk",
        session_token="st-token",
    )
    headers = sigv4.sign_request(
        config, creds, method="POST", url="https://bedrock.test/invoke"
    )
    assert headers["x-amz-security-token"] == "st-token"


# --- mtls / custom ------------------------------------------------------------------


@pytest.mark.unit
def test_mtls_requires_cert_material() -> None:
    with pytest.raises(ConnectionConfigError) as excinfo:
        mtls.build_ssl_context(mtls.MTLSConfig(), _creds(AuthScheme.MTLS))
    assert "client_cert" in str(excinfo.value)


@pytest.mark.unit
def test_custom_auth_must_be_async() -> None:
    def not_async(**kwargs: object) -> None: ...

    with pytest.raises(ConnectionConfigError):
        validate_custom_auth(not_async, where="auth.py")

    async def ok(**kwargs: object) -> None: ...

    assert validate_custom_auth(ok, where="auth.py") is ok


# --- redactor ------------------------------------------------------------------------


@pytest.mark.unit
def test_redactor_is_allowlist_only() -> None:
    config = {
        "base_url": "https://svc.test",
        "timeout_s": 10,
        "account": "acct-1",
    }
    assert redact_config(config, ["base_url", "timeout_s"]) == {
        "base_url": "https://svc.test",
        "timeout_s": 10,
    }
    assert redact_config(config, []) == {}


@pytest.mark.unit
def test_redactor_drops_allowlisted_but_secret_looking_fields() -> None:
    config = {
        "base_url": "https://svc.test",
        "api_key": "oops-a-key",           # denylisted key name
        "note": "sk-ant-abcdefghijklmnop",  # secret-pattern value
    }
    redacted = redact_config(config, ["base_url", "api_key", "note"])
    assert redacted == {"base_url": "https://svc.test"}


@pytest.mark.unit
def test_looks_secret_patterns() -> None:
    assert looks_secret("password", "x")
    assert looks_secret("note", "AKIAABCDEFGHIJKLMNOP")
    assert looks_secret("pem", "-----BEGIN RSA PRIVATE KEY-----")
    assert not looks_secret("base_url", "https://svc.test")
