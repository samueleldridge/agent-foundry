# local_rerank (v1) — reranker stage

Fully local rerank stage (docs/25 § Rerankers). Same 5-file shape as
retriever templates, with `kind: reranker`; bound via
`RetrieverBinding.reranker`. Deterministic lexical-overlap scoring — no
connection slot, no API key, zero egress. It exists so single-key example
projects (rag_hello runs on `OPENAI_API_KEY` alone) still demonstrate the
full retrieve → rerank pipeline; swap in `catalog/cohere_rerank` (or a
served cross-encoder) when you need semantic reranking quality.

## Binding example

```yaml
retrievers:
  - slot: knowledge_base
    ref: catalog/hybrid_rrf
    version: v1
    # ...
    reranker:
      ref: catalog/local_rerank
      version: v1
      top_k: 3          # no connection_bindings — nothing to bind
```

## Contract

- Score = fraction of the query's tokens present in the document
  (`[0, 1]`); ties keep the incoming order, so output is stable and
  reproducible.
- Input order otherwise ignored; truncated to `top_k`;
  id/text/source/metadata preserved.
- Never introduces documents (docs/25 invariant 2).
- `rerank` event carries candidates, latency, before/after ids, and
  `cost_estimate_usd` — always `0`: local reranking is free.

## Gotchas

- This is lexical, not semantic: paraphrases score 0. It is an honest
  demo/dev stage, not a quality upgrade over the hybrid retriever's RRF
  order — production systems should prefer a hosted or served
  cross-encoder.
- `min_token_length` (default 2) drops single-character tokens; raise it
  to 3-4 to also ignore short stop-words ("of", "the").
