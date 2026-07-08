# docs_dense (v1) — project-local

In-process dense retriever over `corpus.json` (project root). Embeds the
corpus lazily on the first retrieve — a run that semantic-cache-hits never
pays for corpus embedding. Dimension agreement between `embedder_binding`
and `dimensions` is enforced at load.

Dev-scale by design; swap for `catalog/pgvector_dense` when the corpus
outgrows memory.
