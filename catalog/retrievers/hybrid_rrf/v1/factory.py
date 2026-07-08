"""Factory for hybrid_rrf@v1: HybridRetriever over the prepared branches.

The wiring prepares + builds the dense/sparse sub-retrievers declared in
config and hands them in via ctx.sub_retrievers.
"""

from foundry.core.errors import CompileError
from foundry.retrieval import HybridRetriever, RetrieverBuildContext


async def build_retriever(
    config,  # HybridRRFConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> HybridRetriever:
    missing = [b for b in ("dense", "sparse") if b not in ctx.sub_retrievers]
    if missing:
        raise CompileError(
            f"hybrid_rrf factory expected prepared sub-retrievers for "
            f"{missing}; the wiring did not supply them",
            context={"slot": ctx.slot, "missing_branches": missing},
        )
    return HybridRetriever(
        ctx.slot,
        ctx.sub_retrievers["dense"],
        ctx.sub_retrievers["sparse"],
        rrf_k=config.rrf_k,
        emit=ctx.emit,
        agent_name=ctx.agent_name,
    )
