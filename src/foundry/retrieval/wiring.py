"""Retriever-binding wiring: compile-time validation + run-time construction
(docs/25 § Configuration, docs/03 § Phase 2b deliverable 10).

Compile time (``prepare_retriever``): resolve the artifact ref, validate the
binding config against the version's config schema, wire connection slots
(shared checks with tools), resolve the embedder binding and enforce the
dimension match against the vector store's configured dimensions — all
before any call (``EmbedderConfigError`` at LOAD). Hybrid retrievers recurse
into their dense/sparse branches; reranker bindings resolve the same way
with ``kind: reranker`` enforced.

Run time (``build_retriever_accessor``): call each artifact's async factory
with a ``RetrieverBuildContext`` (embedder, pooled connections accessor,
sub-retrievers, emit) and wrap the result in a ``RetrieverPipeline`` that
applies the optional rerank stage with docs/25's fall-through-on-failure
semantics.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from foundry.catalog.loader import LoadedRetrieverVersion, load_retriever_version
from foundry.config import (
    ArtifactRef,
    FoundryRoots,
    RerankerBinding,
    RetrieverBinding,
)
from foundry.connections import (
    InProcessConnectionPool,
    PreparedConnection,
    SlotConnectionAccessor,
    validate_connection_slot_wiring,
)
from foundry.core import (
    ConnectionContext,
    Embedder,
    Reranker,
    RetrievedDocument,
    Retriever,
    WarningEvent,
)
from foundry.core.errors import (
    CompileError,
    EmbedderConfigError,
    RerankError,
    RetrievalError,
)
from foundry.core.errors import (
    ConnectionError as FoundryConnectionError,
)
from foundry.core.tool import EmitFn
from foundry.providers._registry import SecretsResolver
from foundry.providers.embedders import (
    EmbedderBinding,
    embedder_capabilities,
    load_embedder,
)

_HYBRID_BRANCHES = ("dense", "sparse")


# --- compile time ---------------------------------------------------------------


@dataclass(frozen=True)
class PreparedReranker:
    binding: RerankerBinding
    ref: ArtifactRef
    loaded: LoadedRetrieverVersion
    config: BaseModel
    wired_connections: dict[str, PreparedConnection]


@dataclass(frozen=True)
class PreparedRetriever:
    slot: str
    ref: ArtifactRef
    loaded: LoadedRetrieverVersion
    config: BaseModel
    wired_connections: dict[str, PreparedConnection]
    embedder_binding: EmbedderBinding | None
    top_k: int
    sub: dict[str, PreparedRetriever] = field(default_factory=dict)
    reranker: PreparedReranker | None = None


def _validate_artifact_config(
    kind_label: str,
    ref: ArtifactRef,
    config_model: type[BaseModel],
    raw_config: dict[str, Any],
    *,
    pointer: str,
    config_file: Path | None,
) -> BaseModel:
    try:
        return config_model.model_validate(raw_config)
    except ValidationError as exc:
        missing = sorted(
            "/".join(str(p) for p in e["loc"])
            for e in exc.errors()
            if e["type"] == "missing"
        )
        unexpected = sorted(
            str(e["loc"][-1]) for e in exc.errors() if e["type"] == "extra_forbidden"
        )
        detail_bits = []
        if missing:
            detail_bits.append(f"missing required fields: {', '.join(missing)}")
        if unexpected:
            detail_bits.append(f"unexpected fields: {', '.join(unexpected)}")
        if not detail_bits:
            first = exc.errors()[0]
            detail_bits.append(
                f"{'/'.join(str(p) for p in first['loc'])}: {first['msg']}"
            )
        raise CompileError(
            f"{kind_label} config incompatible with {ref.to_str()}.\n"
            + (f"  file: {config_file}\n" if config_file else "")
            + f"  pointer: {pointer}\n"
            + "".join(f"  {bit}\n" for bit in detail_bits),
            context={
                "artifact": ref.to_str(),
                "missing_fields": missing,
                "unexpected_fields": unexpected,
            },
            cause=exc,
        ) from exc


def _resolve_embedder_binding(
    config: BaseModel, slot: str, *, config_file: Path | None
) -> EmbedderBinding | None:
    raw = getattr(config, "embedder_binding", None)
    if raw is None:
        return None
    if isinstance(raw, EmbedderBinding):
        return raw
    try:
        if isinstance(raw, BaseModel):
            return EmbedderBinding.model_validate(raw.model_dump())
        return EmbedderBinding.model_validate(raw)
    except ValidationError as exc:
        raise CompileError(
            f"retriever slot {slot!r}: embedder_binding is not a valid "
            f"EmbedderBinding: {exc.errors()[0]['msg']}",
            context={"slot": slot, "file": str(config_file)},
            cause=exc,
        ) from exc


def _expected_store_dimensions(
    config: BaseModel, wired: dict[str, PreparedConnection]
) -> tuple[int | None, str | None]:
    """The vector store's configured dimensionality: an explicit `dimensions`
    field on the retriever config wins; otherwise any wired connection whose
    validated config carries `embedding_dimensions` (e.g. catalog/pgvector)."""
    explicit = getattr(config, "dimensions", None)
    if explicit is not None:
        return int(explicit), "retriever config field 'dimensions'"
    for name, prepared in sorted(wired.items()):
        dims = getattr(prepared.config, "embedding_dimensions", None)
        if dims is not None:
            return int(dims), (
                f"connection {name!r} ({prepared.canonical_ref}) "
                "config field 'embedding_dimensions'"
            )
    return None, None


def _check_dimensions(
    slot: str,
    binding: EmbedderBinding,
    config: BaseModel,
    wired: dict[str, PreparedConnection],
) -> None:
    capabilities = embedder_capabilities(binding.provider, binding.model)
    expected, source = _expected_store_dimensions(config, wired)
    if expected is None or expected == capabilities.dimensions:
        return
    raise EmbedderConfigError(
        f"retriever slot {slot!r}: dimension mismatch — embedder "
        f"{binding.provider}/{binding.model} produces "
        f"{capabilities.dimensions}-dimensional vectors but the vector store "
        f"is configured for {expected} (from {source}). Re-index the store "
        "or switch embedders (docs/24 § Dimension compatibility).",
        context={
            "slot": slot,
            "embedder": f"{binding.provider}/{binding.model}",
            "embedder_dimensions": capabilities.dimensions,
            "store_dimensions": expected,
            "store_dimensions_source": source,
        },
    )


def prepare_reranker(
    slot: str,
    binding: RerankerBinding,
    roots: FoundryRoots,
    prepared_connections: dict[str, PreparedConnection],
    *,
    config_file: Path | None = None,
) -> PreparedReranker:
    ref = ArtifactRef.parse(binding.ref, "retriever", version=binding.version)
    loaded = load_retriever_version(ref, roots)
    if loaded.spec.kind != "reranker":
        raise CompileError(
            f"retriever slot {slot!r}: reranker ref {ref.to_str()!r} is a "
            f"{loaded.spec.kind!r} artifact, not a reranker",
            context={"slot": slot, "ref": ref.to_str(),
                     "kind": loaded.spec.kind},
        )
    config = _validate_artifact_config(
        "RerankerBinding",
        ref,
        loaded.config_model,
        binding.config,
        pointer=f"/retrievers/{slot}/reranker/config",
        config_file=config_file,
    )
    wired = validate_connection_slot_wiring(
        owner_kind="Reranker",
        owner_name=f"{slot}.reranker",
        declared_slots=loaded.spec.connections_required,
        connection_bindings=binding.connection_bindings,
        prepared=prepared_connections,
        pointer_prefix=f"/retrievers/{slot}/reranker",
        config_file=config_file,
    )
    return PreparedReranker(
        binding=binding, ref=ref, loaded=loaded, config=config,
        wired_connections=wired,
    )


def prepare_retriever(
    binding: RetrieverBinding,
    roots: FoundryRoots,
    prepared_connections: dict[str, PreparedConnection],
    *,
    config_file: Path | None = None,
) -> PreparedRetriever:
    """Compile-time preparation of one RetrieverBinding (recursing into
    hybrid branches). Every error here is a load-time error by construction."""
    slot = binding.slot
    ref = ArtifactRef.parse(binding.ref, "retriever", version=binding.version)
    loaded = load_retriever_version(ref, roots)
    if loaded.spec.kind == "reranker":
        raise CompileError(
            f"retriever slot {slot!r}: {ref.to_str()!r} is a reranker "
            "artifact; bind it under `reranker:`, not as the retriever",
            context={"slot": slot, "ref": ref.to_str()},
        )
    config = _validate_artifact_config(
        "RetrieverBinding",
        ref,
        loaded.config_model,
        binding.config,
        pointer=f"/retrievers/{slot}/config",
        config_file=config_file,
    )
    wired = validate_connection_slot_wiring(
        owner_kind="Retriever",
        owner_name=slot,
        declared_slots=loaded.spec.connections_required,
        connection_bindings=binding.connection_bindings,
        prepared=prepared_connections,
        pointer_prefix=f"/retrievers/{slot}",
        config_file=config_file,
    )

    embedder_binding = _resolve_embedder_binding(config, slot, config_file=config_file)
    if embedder_binding is not None:
        # Load-time capability + dimension check (exit gate: EmbedderConfigError
        # at LOAD, before any embedding call is made).
        _check_dimensions(slot, embedder_binding, config, wired)

    sub: dict[str, PreparedRetriever] = {}
    if loaded.spec.kind == "hybrid":
        for branch in _HYBRID_BRANCHES:
            raw_branch = getattr(config, branch, None)
            if raw_branch is None:
                raise CompileError(
                    f"hybrid retriever slot {slot!r}: config must declare a "
                    f"{branch!r} branch (ref/version/config/"
                    "connection_bindings)",
                    context={"slot": slot, "missing_branch": branch},
                )
            data = (
                raw_branch.model_dump()
                if isinstance(raw_branch, BaseModel)
                else dict(raw_branch)
            )
            try:
                sub_binding = RetrieverBinding(
                    slot=f"{slot}_{branch}",
                    ref=str(data["ref"]),
                    version=str(data["version"]),
                    config=dict(data.get("config") or {}),
                    connection_bindings=dict(data.get("connection_bindings") or {}),
                    top_k=binding.top_k,
                )
            except (KeyError, ValidationError) as exc:
                raise CompileError(
                    f"hybrid retriever slot {slot!r}: branch {branch!r} is not "
                    f"a valid sub-retriever binding: {exc}",
                    context={"slot": slot, "branch": branch},
                    cause=exc if isinstance(exc, Exception) else None,
                ) from exc
            sub[branch] = prepare_retriever(
                sub_binding, roots, prepared_connections, config_file=config_file
            )

    reranker = (
        prepare_reranker(
            slot, binding.reranker, roots, prepared_connections,
            config_file=config_file,
        )
        if binding.reranker is not None
        else None
    )
    return PreparedRetriever(
        slot=slot,
        ref=ref,
        loaded=loaded,
        config=config,
        wired_connections=wired,
        embedder_binding=embedder_binding,
        top_k=binding.top_k,
        sub=sub,
        reranker=reranker,
    )


def prepare_retrievers(
    bindings: list[RetrieverBinding],
    roots: FoundryRoots,
    prepared_connections: dict[str, PreparedConnection],
    *,
    config_file: Path | None = None,
) -> dict[str, PreparedRetriever]:
    return {
        binding.slot: prepare_retriever(
            binding, roots, prepared_connections, config_file=config_file
        )
        for binding in bindings
    }


# --- run time ---------------------------------------------------------------------


@dataclass
class RetrieverBuildContext:
    """What a retriever/reranker factory receives (docs/25 § Catalog template
    details: 'builds the concrete Retriever given configs + connection
    handles')."""

    project: str
    slot: str
    version_dir: Path
    project_dir: Path
    default_top_k: int
    embedder: Embedder | None = None
    connections: SlotConnectionAccessor | None = None
    sub_retrievers: dict[str, Retriever] = field(default_factory=dict)
    emit: EmitFn | None = None
    agent_name: str = ""
    http_transport: httpx.AsyncBaseTransport | None = None


class RetrieverPipeline:
    """Retriever + optional rerank stage, bound to one agent slot.

    Reranker failures fall through with the unreranked docs + a warning
    event (docs/25 § Failure modes) — never a run failure.
    """

    def __init__(
        self,
        slot: str,
        retriever: Retriever,
        reranker: Reranker | None,
        *,
        default_top_k: int,
        reranker_top_k: int | None,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = slot
        self.kind = retriever.kind
        self.retriever = retriever
        self.reranker = reranker
        self._default_top_k = default_top_k
        self._reranker_top_k = reranker_top_k
        self._emit = emit
        self._agent_name = agent_name

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        documents = await self.retriever.retrieve(
            query, top_k or self._default_top_k, filters
        )
        if self.reranker is None or not documents:
            return documents
        try:
            return await self.reranker.rerank(
                query, documents, self._reranker_top_k
            )
        except (RerankError, FoundryConnectionError) as exc:
            if self._emit is not None:
                self._emit(
                    WarningEvent,
                    agent_name=self._agent_name,
                    category="rerank.fallthrough",
                    message=f"reranker for slot {self.name!r} failed; "
                    f"returning unreranked documents: {exc}",
                    error_class=type(exc).__name__,
                )
            return documents


class MappingRetrieverAccessor:
    """Concrete ``RetrieverAccessor``: slot → pipeline."""

    def __init__(self, pipelines: dict[str, RetrieverPipeline]) -> None:
        self._pipelines = pipelines

    def get(self, slot: str) -> RetrieverPipeline:
        pipeline = self._pipelines.get(slot)
        if pipeline is None:
            raise RetrievalError(
                f"no retriever bound to slot {slot!r}; declared slots: "
                f"{', '.join(sorted(self._pipelines)) or '(none)'}",
                context={"slot": slot,
                         "declared_slots": sorted(self._pipelines)},
            )
        return pipeline

    def slots(self) -> list[str]:
        return sorted(self._pipelines)


async def _call_factory(
    prepared: PreparedRetriever | PreparedReranker,
    config: BaseModel,
    ctx: RetrieverBuildContext,
) -> Any:
    factory = prepared.loaded.factory
    result = factory(config, ctx)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _build_one(
    prepared: PreparedRetriever,
    *,
    pool: InProcessConnectionPool,
    project: str,
    project_dir: Path,
    agent_name: str,
    secrets: SecretsResolver | None,
    transport: httpx.AsyncBaseTransport | None,
    emit: EmitFn | None,
    accessors: list[SlotConnectionAccessor],
) -> RetrieverPipeline:
    sub_retrievers: dict[str, Retriever] = {}
    for branch, sub_prepared in prepared.sub.items():
        sub_retrievers[branch] = await _build_one(
            sub_prepared,
            pool=pool,
            project=project,
            project_dir=project_dir,
            agent_name=agent_name,
            secrets=secrets,
            transport=transport,
            emit=emit,
            accessors=accessors,
        )

    accessor = SlotConnectionAccessor(
        pool,
        project,
        prepared.wired_connections,
        ConnectionContext(http_transport=transport),
        agent_name=agent_name,
        emit=emit,
    )
    accessors.append(accessor)
    embedder = (
        load_embedder(prepared.embedder_binding, secrets, transport=transport)
        if prepared.embedder_binding is not None
        else None
    )
    ctx = RetrieverBuildContext(
        project=project,
        slot=prepared.slot,
        version_dir=prepared.loaded.directory,
        project_dir=project_dir,
        default_top_k=prepared.top_k,
        embedder=embedder,
        connections=accessor,
        sub_retrievers=sub_retrievers,
        emit=emit,
        agent_name=agent_name,
        http_transport=transport,
    )
    retriever = await _call_factory(prepared, prepared.config, ctx)
    if not hasattr(retriever, "retrieve"):
        raise CompileError(
            f"retriever factory for {prepared.ref.to_str()!r} returned "
            f"{type(retriever).__name__}, which has no retrieve() method",
            context={"ref": prepared.ref.to_str(),
                     "returned_type": type(retriever).__name__},
        )

    reranker: Reranker | None = None
    if prepared.reranker is not None:
        reranker_accessor = SlotConnectionAccessor(
            pool,
            project,
            prepared.reranker.wired_connections,
            ConnectionContext(http_transport=transport),
            agent_name=agent_name,
            emit=emit,
        )
        accessors.append(reranker_accessor)
        reranker_ctx = RetrieverBuildContext(
            project=project,
            slot=prepared.slot,
            version_dir=prepared.reranker.loaded.directory,
            project_dir=project_dir,
            default_top_k=prepared.reranker.binding.top_k or prepared.top_k,
            connections=reranker_accessor,
            emit=emit,
            agent_name=agent_name,
            http_transport=transport,
        )
        built = await _call_factory(
            prepared.reranker, prepared.reranker.config, reranker_ctx
        )
        if not hasattr(built, "rerank"):
            raise CompileError(
                f"reranker factory for {prepared.reranker.ref.to_str()!r} "
                f"returned {type(built).__name__}, which has no rerank() method",
                context={"ref": prepared.reranker.ref.to_str(),
                         "returned_type": type(built).__name__},
            )
        reranker = built

    return RetrieverPipeline(
        prepared.slot,
        retriever,
        reranker,
        default_top_k=prepared.top_k,
        reranker_top_k=(
            prepared.reranker.binding.top_k
            if prepared.reranker is not None
            else None
        ),
        emit=emit,
        agent_name=agent_name,
    )


async def build_retriever_accessor(
    prepared: dict[str, PreparedRetriever],
    *,
    pool: InProcessConnectionPool,
    project: str,
    project_dir: Path,
    agent_name: str,
    secrets: SecretsResolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    emit: EmitFn | None = None,
) -> tuple[MappingRetrieverAccessor, list[SlotConnectionAccessor]]:
    """Build every slot's pipeline at run start. Returns the accessor plus
    the connection accessors the runtime must release at run end."""
    accessors: list[SlotConnectionAccessor] = []
    pipelines: dict[str, RetrieverPipeline] = {}
    for slot, one in prepared.items():
        pipelines[slot] = await _build_one(
            one,
            pool=pool,
            project=project,
            project_dir=project_dir,
            agent_name=agent_name,
            secrets=secrets,
            transport=transport,
            emit=emit,
            accessors=accessors,
        )
    return MappingRetrieverAccessor(pipelines), accessors


__all__ = [
    "MappingRetrieverAccessor",
    "PreparedReranker",
    "PreparedRetriever",
    "RetrieverBuildContext",
    "RetrieverPipeline",
    "build_retriever_accessor",
    "prepare_reranker",
    "prepare_retriever",
    "prepare_retrievers",
]
