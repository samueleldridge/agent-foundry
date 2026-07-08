# docs_sparse (v1) — project-local

In-process BM25 retriever over `corpus.json`. Catches exact terms the dense
branch paraphrases away (ids like `FR-001`, proper names). Dev-scale; swap
for an Elasticsearch/OpenSearch-backed template in production.
