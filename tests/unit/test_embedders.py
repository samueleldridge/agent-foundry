"""Embedder adapter family (docs/11 § Embedders, docs/24 § Embedder
abstraction) — request shapes, advertised dimensions, batching, error
classification, and the registry, all over httpx.MockTransport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from foundry.core.errors import (
    EmbedderAuthError,
    EmbedderConfigError,
    EmbedderUnexpectedError,
)
from foundry.providers.embedders import (
    EmbedderBinding,
    EmbedderSettings,
    available_embedders,
    embedder_capabilities,
    load_embedder,
)


def _binding(provider: str, model: str, **settings: Any) -> EmbedderBinding:
    return EmbedderBinding(
        provider=provider,
        model=model,
        settings=EmbedderSettings(
            retry_policy={"initial_delay_s": 0.01, "max_delay_s": 0.02},  # type: ignore[arg-type]
            **settings,
        ),
    )


class _Recorder:
    def __init__(self, respond: Any) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request, len(self.requests))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _openai_style(dims: int) -> Any:
    def respond(request: httpx.Request, _n: int) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "data": [
                {"index": i, "embedding": [0.1] * dims}
                for i in range(len(body["input"]))
            ],
            "usage": {"prompt_tokens": 5 * len(body["input"])},
        })
    return respond


def _voyage_style(dims: int) -> Any:
    def respond(request: httpx.Request, _n: int) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "data": [
                {"index": i, "embedding": [0.2] * dims}
                for i in range(len(body["input"]))
            ],
            "usage": {"total_tokens": 7 * len(body["input"])},
        })
    return respond


# --- exit gate: round-trip with advertised dimensions ---------------------------


@pytest.mark.unit
async def test_voyage_3_round_trip_produces_advertised_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-voyage")
    recorder = _Recorder(_voyage_style(1024))
    embedder = load_embedder(
        _binding("voyage", "voyage-3"), transport=recorder.transport()
    )
    out = await embedder.embed(["hello world"], "query")
    assert len(out) == 1
    assert len(out[0].vector) == 1024 == embedder.capabilities.dimensions
    assert out[0].dimensions == 1024
    assert out[0].input_tokens == 7
    assert out[0].cost_estimate_usd is not None
    # asymmetric: purpose reaches the wire as input_type
    body = json.loads(recorder.requests[0].content)
    assert body["input_type"] == "query"
    assert body["model"] == "voyage-3"
    assert recorder.requests[0].headers["Authorization"] == "Bearer fake-voyage"


@pytest.mark.unit
async def test_openai_small_round_trip_produces_advertised_dimensions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai")
    recorder = _Recorder(_openai_style(1536))
    embedder = load_embedder(
        _binding("openai", "text-embedding-3-small"),
        transport=recorder.transport(),
    )
    out = await embedder.embed(["hello world"])
    assert len(out[0].vector) == 1536 == embedder.capabilities.dimensions
    body = json.loads(recorder.requests[0].content)
    assert "input_type" not in body  # symmetric embedder


@pytest.mark.unit
async def test_cohere_maps_purpose_to_search_input_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COHERE_API_KEY", "fake-cohere")

    def respond(request: httpx.Request, _n: int) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "embeddings": [[0.3] * 1024 for _ in body["texts"]],
            "meta": {"billed_units": {"input_tokens": 4 * len(body["texts"])}},
        })

    recorder = _Recorder(respond)
    embedder = load_embedder(
        _binding("cohere", "embed-english-v3.0"), transport=recorder.transport()
    )
    out = await embedder.embed(["a", "b"], "document")
    assert [len(e.vector) for e in out] == [1024, 1024]
    body = json.loads(recorder.requests[0].content)
    assert body["input_type"] == "search_document"


# --- batching --------------------------------------------------------------------


@pytest.mark.unit
async def test_inputs_are_chunked_into_batches_and_reassembled_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    def respond(request: httpx.Request, _n: int) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={
            "data": [
                # encode the text back into the vector so order is checkable
                {"index": i, "embedding": [float(t)] * 1024}
                for i, t in enumerate(body["input"])
            ],
            "usage": {"total_tokens": len(body["input"])},
        })

    recorder = _Recorder(respond)
    embedder = load_embedder(
        _binding("voyage", "voyage-3", batch_size=2),
        transport=recorder.transport(),
    )
    out = await embedder.embed(["0", "1", "2", "3", "4"])
    assert len(recorder.requests) == 3  # 2 + 2 + 1
    assert [e.vector[0] for e in out] == [0.0, 1.0, 2.0, 3.0, 4.0]


# --- error classification -----------------------------------------------------------


@pytest.mark.unit
async def test_401_becomes_embedder_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "bad-key")
    recorder = _Recorder(
        lambda _r, _n: httpx.Response(401, json={"error": "bad key"})
    )
    embedder = load_embedder(
        _binding("voyage", "voyage-3"), transport=recorder.transport()
    )
    with pytest.raises(EmbedderAuthError):
        await embedder.embed(["x"])


@pytest.mark.unit
async def test_429_is_retried_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")

    def respond(request: httpx.Request, n: int) -> httpx.Response:
        if n == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return _voyage_style(1024)(request, n)

    recorder = _Recorder(respond)
    embedder = load_embedder(
        _binding("voyage", "voyage-3"), transport=recorder.transport()
    )
    out = await embedder.embed(["x"])
    assert len(recorder.requests) == 2
    assert len(out[0].vector) == 1024


@pytest.mark.unit
async def test_dimension_drift_from_provider_raises_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    recorder = _Recorder(_voyage_style(999))  # manifest says 1024
    embedder = load_embedder(
        _binding("voyage", "voyage-3"), transport=recorder.transport()
    )
    with pytest.raises(EmbedderUnexpectedError) as excinfo:
        await embedder.embed(["x"])
    assert excinfo.value.context["received_dims"] == 999
    assert excinfo.value.context["advertised_dims"] == 1024


# --- registry -----------------------------------------------------------------------


@pytest.mark.unit
def test_registry_lists_all_four_providers() -> None:
    assert available_embedders() == ["bedrock", "cohere", "openai", "voyage"]


@pytest.mark.unit
def test_unknown_provider_and_model_raise_config_errors() -> None:
    with pytest.raises(EmbedderConfigError) as excinfo:
        embedder_capabilities("hal9000", "x")
    assert "hal9000" in str(excinfo.value)
    with pytest.raises(EmbedderConfigError) as excinfo:
        embedder_capabilities("voyage", "voyage-99")
    assert "voyage-3" in str(excinfo.value)  # names what IS available


@pytest.mark.unit
async def test_bedrock_stub_loads_but_embed_raises_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "example-profile")
    # capabilities resolve (dimension checks can run at compile time)...
    assert embedder_capabilities("bedrock", "amazon.titan-embed-text-v2").dimensions == 1024
    embedder = load_embedder(_binding("bedrock", "amazon.titan-embed-text-v2"))
    # ...but calls fail loudly, naming the missing dependency.
    with pytest.raises(EmbedderConfigError) as excinfo:
        await embedder.embed(["x"])
    assert excinfo.value.context["missing_dependency"] == "boto3"
