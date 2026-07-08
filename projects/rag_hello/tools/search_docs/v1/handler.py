"""Handler for search_docs@v1 — retrieval-as-a-tool (docs/25 § Pattern A)."""

from schemas import SearchIn, SearchOut

from foundry.core.errors import RetrievalError


async def handle(inputs: SearchIn, ctx) -> SearchOut:
    if ctx.retrievers is None:
        raise RetrievalError(
            "search_docs needs a retriever bound to slot 'knowledge_base' "
            "on the calling agent",
            context={"tool": "search_docs"},
        )
    retriever = ctx.retrievers.get("knowledge_base")
    documents = await retriever.retrieve(inputs.query, inputs.top_k)
    return SearchOut(documents=[d.model_dump(mode="json") for d in documents])
