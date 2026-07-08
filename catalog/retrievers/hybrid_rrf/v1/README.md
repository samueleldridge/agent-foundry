# hybrid_rrf (v1)

Hybrid retriever template: dense + sparse branches fanned out in parallel,
merged via Reciprocal Rank Fusion (docs/25 § Hybrid retrieval).

## Binding example

```yaml
retrievers:
  - slot: knowledge_base
    ref: catalog/hybrid_rrf
    version: v1
    config:
      dense:
        ref: catalog/pgvector_dense
        version: v1
        config:
          embedder_binding: {provider: voyage, model: voyage-3}
          table: documents
        connection_bindings: {dense_store: prod_pgvector}
      sparse:
        ref: local/docs_sparse
        version: v1
        config: {corpus_path: corpus.json}
      rrf_k: 60
    top_k: 50
    reranker:
      ref: catalog/cohere_rerank
      version: v1
      connection_bindings: {cohere: cohere_api}
      top_k: 8
```

## Contract

- RRF: `score(doc) = sum over branches of 1 / (rrf_k + rank)`, 1-based ranks.
  Rank-based — no score normalisation across branches.
- Deduplication by document id; text/metadata come from the dense branch
  when both returned the doc.
- Fail-degradation (docs/25): one branch failing → other branch's results +
  a `warning` event naming the failed branch. Both failing → RetrievalError.
- The `retrieval` event carries per-branch latencies (`branch_latency_ms`)
  and `branches_failed`.

## Gotchas

- Sub-retriever `connection_bindings` name connections in the PROJECT's
  system.yaml — the branches go through the same compile-time slot checks
  as top-level retrievers.
- `top_k` applies to the merged output; each branch is queried with the
  same `top_k` before fusion.
