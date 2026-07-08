# episode_store@v1

In-process BM25 retriever over `episodes.json` — the corpus behind
memory_hello's episodic memory layer. No connections, no embedder, no
infra. Exposes `ingest(texts)` so the episodic layer's per-turn writes
land in the same index (visible to later turns in the same run).
