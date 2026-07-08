# pgvector_dense (v1)

Dense retriever template: `EmbedderBinding` + `catalog/pgvector` connection
(docs/25 § Catalog templates).

## Binding example

```yaml
retrievers:
  - slot: knowledge_base
    ref: catalog/pgvector_dense
    version: v1
    connection_bindings:
      dense_store: prod_pgvector
    config:
      embedder_binding: {provider: voyage, model: voyage-3}
      table: documents
    top_k: 50
```

## Contract

- Query embedded with `purpose="query"`; documents are assumed ingested with
  the SAME embedder model and `purpose="document"`.
- Compile-time dimension check: the embedder's dimensions must equal the
  bound connection's `embedding_dimensions` — mismatch fails load with
  `EmbedderConfigError`.
- `score` is cosine similarity (`1 - (embedding <=> query)`).
- Filters are not supported in v1 (structured error if passed).

## Gotchas

- Swapping `embedder_binding` requires re-indexing the corpus; the dimension
  check catches incompatible swaps, not semantically drifted ones.
- Ingestion is deliberately out of scope (docs/25 open question 1 —
  RESOLVED: no ingestion primitive in v1). Chunk → embed → `INSERT` per your
  corpus pipeline.
