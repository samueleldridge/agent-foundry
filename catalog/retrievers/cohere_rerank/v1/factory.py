"""Factory for cohere_rerank@v1: CohereReranker over the pooled connection."""

import httpx

from foundry.retrieval import CohereReranker, RetrieverBuildContext


async def build_reranker(
    config,  # CohereRerankStageConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> CohereReranker:
    assert ctx.connections is not None
    connection = await ctx.connections.get("cohere")
    model = getattr(connection, "model", None) or config.model

    async def get_client() -> httpx.AsyncClient:
        conn = await ctx.connections.get("cohere")  # pooled: cache hit
        return conn.client

    return CohereReranker(
        model,
        get_client,
        price_per_call_usd=config.price_per_call_usd,
        emit=ctx.emit,
        agent_name=ctx.agent_name,
    )
