# search_docs (v1) — project-local

Retrieval-as-a-tool (docs/25 § Pattern A): the LLM decides when to search;
the handler runs `ctx.retrievers.get("knowledge_base")` — the hybrid
dense+sparse RRF pipeline with a Cohere rerank stage.

Cacheable (`cache_ttl_s: 300`, project scope): the corpus is static on that
timescale, so repeated identical queries — including two calls in one run —
hit the tool-result cache instead of re-retrieving.
