"""EmbedderAdapter — abstract base for the embedder adapter family
(docs/11 § EmbedderAdapter base).

Mirrors ``ProviderAdapter``'s direct-httpx design: concrete adapters
implement ``_build_request`` + ``_parse_response``; the base owns batching,
concurrency, per-attempt timeouts, retries, error classification, and cost
estimation. Embedders have no output tokens — cost is input-tokens x
``EmbedderPricing.input_per_1m``.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar, Literal

import httpx

from foundry.core import (
    EmbedderCapabilities,
    Embedding,
    ResolvedCredentials,
)
from foundry.core.errors import (
    EmbedderAuthError,
    EmbedderError,
    EmbedderTimeoutError,
    EmbedderUnexpectedError,
)
from foundry.providers._base import HttpRequestSpec
from foundry.providers.embedders._types import EmbedderSettings

Purpose = Literal["query", "document"]


@dataclass(frozen=True)
class ParsedEmbedBatch:
    """What a concrete adapter's ``_parse_response`` returns."""

    vectors: list[list[float]]
    input_tokens: int
    """Provider-billed input tokens for the whole batch (0 if unreported)."""


class EmbedderAdapter(ABC):
    """Abstract base every concrete embedder subclasses."""

    provider_name: ClassVar[str]
    default_credentials_env: ClassVar[str]

    def __init__(
        self,
        model: str,
        capabilities: EmbedderCapabilities,
        credentials: ResolvedCredentials,
        settings: EmbedderSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.capabilities = capabilities
        self._credentials = credentials
        self._settings = settings or EmbedderSettings()
        self._transport = transport

    @property
    def name(self) -> str:
        """'provider:model' — the observability identity (docs/10 EmbedCall)."""
        return f"{self.provider_name}:{self.model}"

    # --- hooks concrete adapters implement -----------------------------------

    @abstractmethod
    def _build_request(self, texts: list[str], purpose: Purpose) -> HttpRequestSpec: ...

    @abstractmethod
    def _parse_response(self, payload: dict[str, Any]) -> ParsedEmbedBatch: ...

    # --- public surface -------------------------------------------------------

    async def embed(
        self,
        inputs: list[str],
        purpose: Purpose = "document",
    ) -> list[Embedding]:
        if not inputs:
            return []
        batch_size = min(self._settings.batch_size, self.capabilities.max_batch_size)
        batches = [
            inputs[i : i + batch_size] for i in range(0, len(inputs), batch_size)
        ]
        results = await asyncio.gather(
            *(self._embed_batch_with_retries(batch, purpose) for batch in batches)
        )
        out: list[Embedding] = []
        for batch_embeddings in results:
            out.extend(batch_embeddings)
        return out

    # --- internals -------------------------------------------------------------

    async def _embed_batch_with_retries(
        self, texts: list[str], purpose: Purpose
    ) -> list[Embedding]:
        policy = self._settings.retry_policy
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._embed_batch(texts, purpose)
            except EmbedderTimeoutError:
                if attempt >= policy.max_attempts:
                    raise
                await asyncio.sleep(policy.delay_for(attempt))

    async def _embed_batch(self, texts: list[str], purpose: Purpose) -> list[Embedding]:
        spec = self._build_request(texts, purpose)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._settings.timeout_s
            ) as client:
                response = await client.post(
                    spec.url, headers=spec.headers, json=spec.body
                )
        except httpx.TimeoutException as exc:
            raise EmbedderTimeoutError(
                f"{self.name} embed call exceeded {self._settings.timeout_s}s",
                context={"embedder": self.name,
                         "timeout_s": self._settings.timeout_s},
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbedderUnexpectedError(
                f"{self.name} transport error: {exc}",
                context={"embedder": self.name},
                cause=exc,
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        payload = _safe_json(response)
        if response.status_code >= 400:
            raise self._classify_http_error(response.status_code, payload)
        try:
            parsed = self._parse_response(payload)
        except EmbedderError:
            raise
        except Exception as exc:
            raise EmbedderUnexpectedError(
                f"{self.name} returned an unparseable embed response: {exc}",
                context={"embedder": self.name},
                cause=exc,
            ) from exc
        if len(parsed.vectors) != len(texts):
            raise EmbedderUnexpectedError(
                f"{self.name} returned {len(parsed.vectors)} vectors for "
                f"{len(texts)} inputs",
                context={"embedder": self.name, "inputs": len(texts),
                         "vectors": len(parsed.vectors)},
            )
        return self._build_embeddings(parsed, latency_ms)

    def _build_embeddings(
        self, parsed: ParsedEmbedBatch, latency_ms: int
    ) -> list[Embedding]:
        expected = self.capabilities.dimensions
        count = len(parsed.vectors)
        tokens_each, remainder = divmod(parsed.input_tokens, count)
        out: list[Embedding] = []
        for index, vector in enumerate(parsed.vectors):
            if len(vector) != expected:
                raise EmbedderUnexpectedError(
                    f"{self.name} returned a {len(vector)}-dimensional vector; "
                    f"the manifest advertises {expected} — manifest drift or a "
                    "provider-side change",
                    context={"embedder": self.name, "received_dims": len(vector),
                             "advertised_dims": expected},
                )
            tokens = tokens_each + (1 if index < remainder else 0)
            out.append(
                Embedding(
                    vector=vector,
                    dimensions=len(vector),
                    model=self.model,
                    input_tokens=tokens,
                    latency_ms=latency_ms,
                    cost_estimate_usd=(
                        Decimal(tokens)
                        * self.capabilities.pricing.input_per_1m
                        / Decimal(1_000_000)
                    ),
                )
            )
        return out

    def _classify_http_error(
        self, status: int, payload: dict[str, Any]
    ) -> EmbedderError:
        message = _embedder_message(payload)
        context: dict[str, Any] = {
            "http_status": status,
            "provider_message": message,
            "embedder": self.name,
        }
        if status in (401, 403):
            return EmbedderAuthError(
                f"{self.name} rejected credentials (HTTP {status}): {message}",
                context=context,
            )
        if status in (408, 429, 504):
            # 429 is grouped with timeouts: both are transient + retried.
            return EmbedderTimeoutError(
                f"{self.name} transient failure (HTTP {status}): {message}",
                context=context,
            )
        return EmbedderUnexpectedError(
            f"{self.name} embed call failed (HTTP {status}): {message}",
            context=context,
        )

    def _api_key(self) -> str:
        secret = self._credentials.secret
        if not secret:
            raise EmbedderAuthError(
                f"{self.name} has no resolved API key "
                f"(set {self.default_credentials_env} or bind credentials_ref)",
                context={"embedder": self.name,
                         "default_env": self.default_credentials_env},
            )
        return secret


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {"raw_text": response.text[:2000]}
    if isinstance(payload, dict):
        return payload
    return {"raw": payload}


def _embedder_message(payload: dict[str, Any]) -> str:
    for key in ("error", "detail", "message"):
        err = payload.get(key)
        if isinstance(err, dict):
            return str(err.get("message", err))
        if err is not None:
            return str(err)
    return str(payload)[:500]


__all__ = ["EmbedderAdapter", "ParsedEmbedBatch", "Purpose"]
