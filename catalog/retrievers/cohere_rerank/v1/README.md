# cohere_rerank (v1) — reranker stage

Cross-encoder rerank stage (docs/25 § Rerankers). Same 5-file shape as
retriever templates, with `kind: reranker`; bound via
`RetrieverBinding.reranker`.

## Binding example

```yaml
retrievers:
  - slot: knowledge_base
    ref: catalog/hybrid_rrf
    version: v1
    # ...
    reranker:
      ref: catalog/cohere_rerank
      version: v1
      connection_bindings:
        cohere: cohere_api        # a catalog/cohere_rerank CONNECTION
      top_k: 8
```

## Contract

- Input order ignored; output `score` is Cohere's relevance score (0..1);
  truncated to `top_k`; id/text/source/metadata preserved.
- Never introduces documents (docs/25 invariant 2).
- `rerank` event carries candidates, latency, before/after ids, and
  `cost_estimate_usd` — ALWAYS populated (defensive 0 when unknown).
- Failures (timeout, 4xx/5xx) surface as RerankError; the retriever
  pipeline falls through with the unreranked docs + a warning event.

## Gotchas

- The rerank model comes from the bound CONNECTION's config; the stage's
  `model` field is only a fallback. Keep them consistent.
- Rerankers bill per query (docs/25 § Cost-aware use) — batch workloads
  multiply fast; watch `foundry.rerank.cost_usd`.
