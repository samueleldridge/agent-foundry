"""Provider / model panel (docs/72 § Provider panel).

Routes:

- ``GET  /providers`` — every provider the foundry ships adapters for,
  with its generation models (capability + pricing manifests, docs/11)
  and embedding models (embedder registry). Bedrock/Azure/Vertex appear
  as stubs with a note explaining why the studio manages no key for them.
- ``GET  /providers/keys`` — per-provider key STATUS: ``{var_name, set,
  source, last4}``. The stored key value never appears in any response.
- ``PUT  /providers/{name}/key`` — store a key server-side.
- ``DELETE /providers/{name}/key`` — remove a studio-stored key
  (env-sourced keys refuse with a structured error).
- ``POST /providers/{name}/key/verify`` — live round-trip using the
  provider's cheapest call (models list, or a one-token embed).

Storage: ``<FOUNDRY_HOME>/studio/credentials.env`` (mode 0600), one
``VAR=value`` line per provider's ``default_credentials_env``. Precedence
mirrors the CLI ``.env`` loader (foundry.cli.dotenv): the REAL process
environment always wins — studio-stored keys are loaded into
``os.environ`` only where the var isn't already set, at studio startup
and on every save. This stays a STUDIO-layer concern: the library proper
never reads credential files (docs/11 § Studio-stored credentials).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import APIRouter

from foundry.cli.dotenv import parse_env_text
from foundry.core import EmbedderCapabilities
from foundry.core.errors import ConfigLoadError, ConfigValidationError
from foundry.providers import (
    CapabilityName,
    ProviderCapabilities,
    all_capabilities,
)
from foundry.providers.anthropic import AnthropicProvider
from foundry.providers.embedders.cohere import COHERE_MODELS, CohereEmbedder
from foundry.providers.embedders.openai import OPENAI_MODELS
from foundry.providers.embedders.voyage import VOYAGE_MODELS, VoyageEmbedder
from foundry.providers.openai import OpenAIProvider
from foundry.storage.paths import foundry_home
from foundry.studio.context import StudioContext
from foundry.studio.events import emit_studio_event
from foundry.studio.schemas import (
    ProviderEmbeddingModelInfo,
    ProviderInfo,
    ProviderKeyRequest,
    ProviderKeyStatus,
    ProviderKeyVerifyResult,
    ProviderModelInfo,
    ProviderModelPricing,
)

_VERIFY_TIMEOUT_S = 20.0
_ANTHROPIC_VERSION = "2023-06-01"


# --- provider table -------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderEntry:
    """Studio-side descriptor of one provider card."""

    name: str
    label: str
    kind: str  # "llm" | "embedder"
    credentials_env: str | None = None
    stub: bool = False
    note: str = ""
    embedding_models: dict[str, EmbedderCapabilities] = field(
        default_factory=dict
    )


PROVIDERS: tuple[ProviderEntry, ...] = (
    ProviderEntry(
        name="anthropic",
        label="Anthropic",
        kind="llm",
        credentials_env=AnthropicProvider.default_credentials_env,
    ),
    ProviderEntry(
        name="openai",
        label="OpenAI",
        kind="llm",
        credentials_env=OpenAIProvider.default_credentials_env,
        embedding_models=dict(OPENAI_MODELS),
    ),
    ProviderEntry(
        name="voyage",
        label="Voyage AI",
        kind="embedder",
        credentials_env=VoyageEmbedder.default_credentials_env,
        embedding_models=dict(VOYAGE_MODELS),
    ),
    ProviderEntry(
        name="cohere",
        label="Cohere",
        kind="embedder",
        credentials_env=CohereEmbedder.default_credentials_env,
        embedding_models=dict(COHERE_MODELS),
    ),
    ProviderEntry(
        name="bedrock",
        label="AWS Bedrock",
        kind="llm",
        stub=True,
        note=(
            "Phase 1 stub. Bedrock authenticates through the AWS "
            "credential chain (AWS_PROFILE / IAM role), not a single API "
            "key, so the studio does not manage credentials for it yet."
        ),
    ),
    ProviderEntry(
        name="azure",
        label="Azure OpenAI",
        kind="llm",
        stub=True,
        note=(
            "Phase 1 stub. Azure OpenAI uses a per-deployment endpoint + "
            "key pair configured on the connection, so the studio does "
            "not manage credentials for it yet."
        ),
    ),
    ProviderEntry(
        name="vertex",
        label="GCP Vertex",
        kind="llm",
        stub=True,
        note=(
            "Phase 1 stub. Vertex authenticates via Application Default "
            "Credentials, not an API key, so the studio does not manage "
            "credentials for it yet."
        ),
    ),
)


def _entry(name: str) -> ProviderEntry:
    for entry in PROVIDERS:
        if entry.name == name:
            return entry
    known = ", ".join(p.name for p in PROVIDERS)
    raise ConfigLoadError(
        f"unknown provider {name!r}; known providers: {known}",
        context={"provider": name, "not_found": True},
    )


def _key_entry(name: str) -> ProviderEntry:
    """The entry, refusing stubs (they have no studio-managed key)."""
    entry = _entry(name)
    if entry.stub or entry.credentials_env is None:
        raise ConfigValidationError(
            f"provider {name!r} has no studio-managed API key: {entry.note}",
            context={"provider": name, "stub": True},
        )
    return entry


# --- credentials store (<FOUNDRY_HOME>/studio/credentials.env) -------------------------


def credentials_path() -> Path:
    return foundry_home() / "studio" / "credentials.env"


def read_stored_credentials() -> dict[str, str]:
    """Parse the studio credentials file (CLI .env grammar; malformed
    lines skipped, never fatal). Missing file → empty dict."""
    path = credentials_path()
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return dict(parse_env_text(text))


def write_stored_credentials(values: dict[str, str]) -> Path:
    """Write ``VAR=value`` lines with owner-only permissions (0600; the
    parent dir is created 0700)."""
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    lines = "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(lines)
    os.chmod(path, 0o600)  # pre-existing files keep 0600 too
    return path


def apply_stored_credentials(ctx: StudioContext) -> list[str]:
    """Load studio-stored keys into ``os.environ`` — only where the var
    isn't already set (the real environment ALWAYS wins, same rule as the
    CLI .env loader). Vars the studio itself set earlier are refreshed.
    Returns the names applied; runs at app build and after every save."""
    applied: list[str] = []
    for name, value in read_stored_credentials().items():
        if name in os.environ and name not in ctx.studio_env_applied:
            continue  # real env var wins
        os.environ[name] = value
        ctx.studio_env_applied.add(name)
        applied.append(name)
    return applied


def _key_status(ctx: StudioContext, entry: ProviderEntry) -> ProviderKeyStatus:
    var = entry.credentials_env or ""
    stored = read_stored_credentials()
    if var in os.environ and var not in ctx.studio_env_applied:
        return ProviderKeyStatus(
            provider=entry.name, var_name=var, set=True, source="environment"
        )
    if var in stored:
        return ProviderKeyStatus(
            provider=entry.name,
            var_name=var,
            set=True,
            source="studio",
            last4=stored[var][-4:],
        )
    return ProviderKeyStatus(provider=entry.name, var_name=var, set=False)


# --- model listings --------------------------------------------------------------------


def _model_info(caps: ProviderCapabilities) -> ProviderModelInfo:
    flags = [name.value for name in CapabilityName if caps.supports(name)]
    return ProviderModelInfo(
        id=caps.model,
        context_window=caps.max_context_tokens,
        max_output_tokens=caps.max_output_tokens,
        capabilities=flags,
        reasoning=caps.extended_thinking or caps.reasoning_effort,
        pricing=ProviderModelPricing(
            input_per_1m=float(caps.pricing.input_per_1m),
            output_per_1m=float(caps.pricing.output_per_1m),
            cache_read_per_1m=float(caps.pricing.cache_read_per_1m),
            cache_write_per_1m=float(caps.pricing.cache_write_per_1m),
        ),
    )


def _provider_info(entry: ProviderEntry) -> ProviderInfo:
    models = sorted(
        (
            _model_info(caps)
            for caps in all_capabilities()
            if caps.provider == entry.name
        ),
        key=lambda m: m.id,
    )
    embedding = [
        ProviderEmbeddingModelInfo(
            id=caps.model,
            dimensions=caps.dimensions,
            max_input_tokens=caps.max_input_tokens,
            max_batch_size=caps.max_batch_size,
            input_per_1m=float(caps.pricing.input_per_1m),
        )
        for _, caps in sorted(entry.embedding_models.items())
    ]
    return ProviderInfo(
        name=entry.name,
        label=entry.label,
        kind="embedder" if entry.kind == "embedder" else "llm",
        stub=entry.stub,
        note=entry.note,
        credentials_env=entry.credentials_env,
        models=models,
        embedding_models=embedding,
    )


# --- key verification ------------------------------------------------------------------


@dataclass(frozen=True)
class _VerifyCall:
    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, object] | None = None


def _verify_call(provider: str, key: str) -> _VerifyCall:
    """The provider's cheapest authenticated round-trip: a models-list
    GET where one exists, else a one-token embed (voyage)."""
    if provider == "anthropic":
        return _VerifyCall(
            method="GET",
            url="https://api.anthropic.com/v1/models?limit=1",
            headers={
                "x-api-key": key,
                "anthropic-version": _ANTHROPIC_VERSION,
            },
        )
    if provider == "openai":
        return _VerifyCall(
            method="GET",
            url="https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
    if provider == "cohere":
        return _VerifyCall(
            method="GET",
            url="https://api.cohere.com/v1/models?page_size=1",
            headers={"Authorization": f"Bearer {key}"},
        )
    if provider == "voyage":
        return _VerifyCall(
            method="POST",
            url="https://api.voyageai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {key}"},
            body={"model": "voyage-3", "input": ["ping"]},
        )
    raise ConfigValidationError(  # unreachable via routes (_key_entry gates)
        f"provider {provider!r} has no verify call",
        context={"provider": provider},
    )


def _scrub(text: str, key: str) -> str:
    """Belt-and-braces: the key value never rides an error detail."""
    return text.replace(key, "[REDACTED]") if key else text


# --- router ----------------------------------------------------------------------------


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/providers", response_model=list[ProviderInfo])
    def list_providers() -> list[ProviderInfo]:
        return [_provider_info(entry) for entry in PROVIDERS]

    @router.get("/providers/keys", response_model=list[ProviderKeyStatus])
    def key_statuses() -> list[ProviderKeyStatus]:
        return [
            _key_status(ctx, entry)
            for entry in PROVIDERS
            if not entry.stub and entry.credentials_env
        ]

    @router.put("/providers/{name}/key", response_model=ProviderKeyStatus)
    def put_key(name: str, body: ProviderKeyRequest) -> ProviderKeyStatus:
        entry = _key_entry(name)
        var = entry.credentials_env or ""
        api_key = body.api_key.strip()
        if not api_key:
            raise ConfigValidationError(
                "api_key must be a non-empty string",
                context={"provider": name, "field": "api_key"},
            )
        stored = read_stored_credentials()
        stored[var] = api_key
        write_stored_credentials(stored)
        apply_stored_credentials(ctx)
        # A project that failed to compile on this missing var recovers
        # on its next request — no studio restart needed.
        ctx.invalidate_all()
        emit_studio_event(  # NEVER carries the key value
            "studio.provider_key_saved",
            operator="studio",
            provider=name,
            env_var=var,
        )
        return _key_status(ctx, entry)

    @router.delete("/providers/{name}/key", response_model=ProviderKeyStatus)
    def delete_key(name: str) -> ProviderKeyStatus:
        entry = _key_entry(name)
        var = entry.credentials_env or ""
        status = _key_status(ctx, entry)
        if status.source == "environment":
            raise ConfigValidationError(
                f"{var} comes from the backend process environment, not "
                "the studio store — the studio cannot delete it. Unset it "
                "where it is defined (shell export, CI secret, or the "
                "backend repo's .env) and restart foundry studio.",
                context={
                    "provider": name,
                    "env_var": var,
                    "source": "environment",
                },
            )
        stored = read_stored_credentials()
        if var not in stored:
            raise ConfigLoadError(
                f"no studio-stored key for provider {name!r}",
                context={"provider": name, "not_found": True},
            )
        del stored[var]
        write_stored_credentials(stored)
        if var in ctx.studio_env_applied:
            os.environ.pop(var, None)
            ctx.studio_env_applied.discard(var)
        ctx.invalidate_all()
        emit_studio_event(
            "studio.provider_key_deleted",
            operator="studio",
            provider=name,
            env_var=var,
        )
        return _key_status(ctx, entry)

    @router.post(
        "/providers/{name}/key/verify",
        response_model=ProviderKeyVerifyResult,
    )
    async def verify_key(name: str) -> ProviderKeyVerifyResult:
        entry = _key_entry(name)
        var = entry.credentials_env or ""
        key = os.environ.get(var, "")
        if not key:
            return ProviderKeyVerifyResult(
                provider=name,
                var_name=var,
                ok=False,
                detail=(
                    f"{var} is not configured — save a key here or set it "
                    "in the backend environment"
                ),
            )
        call = _verify_call(entry.name, key)
        try:
            async with httpx.AsyncClient(
                transport=ctx.transport, timeout=_VERIFY_TIMEOUT_S
            ) as client:
                response = await client.request(
                    call.method, call.url, headers=call.headers, json=call.body
                )
        except httpx.HTTPError as exc:
            return ProviderKeyVerifyResult(
                provider=name,
                var_name=var,
                ok=False,
                detail=_scrub(f"transport error: {exc}", key),
            )
        if response.status_code < 300:
            return ProviderKeyVerifyResult(
                provider=name,
                var_name=var,
                ok=True,
                status_code=response.status_code,
                detail="credentials accepted",
            )
        if response.status_code in (401, 403):
            detail = f"{entry.label} rejected the key (HTTP {response.status_code})"
        else:
            detail = (
                f"unexpected {entry.label} response "
                f"(HTTP {response.status_code})"
            )
        return ProviderKeyVerifyResult(
            provider=name,
            var_name=var,
            ok=False,
            status_code=response.status_code,
            detail=_scrub(detail, key),
        )

    return router


__all__ = [
    "PROVIDERS",
    "ProviderEntry",
    "apply_stored_credentials",
    "build_router",
    "credentials_path",
    "read_stored_credentials",
    "write_stored_credentials",
]
