"""Factory for pgvector_dense@v1: DenseRetriever over an asyncpg pool."""

from typing import Any

from foundry.core import RetrievedDocument
from foundry.core.errors import RetrievalError
from foundry.retrieval import DenseRetriever, RetrieverBuildContext


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(v) for v in vector) + "]"


async def build_retriever(
    config,  # PgVectorDenseConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> DenseRetriever:
    assert ctx.embedder is not None  # guaranteed: config declares the binding
    assert ctx.connections is not None

    columns = [config.id_column, config.text_column]
    if config.source_column:
        columns.append(config.source_column)
    select_list = ", ".join(columns)

    async def search(
        vector: list[float], top_k: int, filters: dict[str, Any] | None
    ) -> list[RetrievedDocument]:
        if filters:
            raise RetrievalError(
                "pgvector_dense@v1 does not support filters yet; drop the "
                "filters argument or extend the template",
                context={"retriever": ctx.slot, "filters": sorted(filters)},
            )
        connection = await ctx.connections.get("dense_store")
        pool = connection.client  # asyncpg.Pool
        rows = await pool.fetch(
            f"SELECT {select_list}, "
            f"1 - ({config.embedding_column} <=> $1::vector) AS similarity "
            f"FROM {config.table} "
            f"ORDER BY {config.embedding_column} <=> $1::vector LIMIT $2",
            _vector_literal(vector),
            top_k,
        )
        return [
            RetrievedDocument(
                id=str(row[config.id_column]),
                text=str(row[config.text_column]),
                score=float(row["similarity"]),
                source=(
                    str(row[config.source_column])
                    if config.source_column
                    else None
                ),
            )
            for row in rows
        ]

    return DenseRetriever(
        ctx.slot, ctx.embedder, search, emit=ctx.emit, agent_name=ctx.agent_name
    )
