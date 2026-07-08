# pgvector (v1)

Postgres + pgvector — the vector-store connection Phase 2b retrievers and
semantic caches bind. `embedding_dimensions` is part of the config contract
so the 2b dimension-match compile check can validate embedder wiring.

Credentials JSON: `{"username": ..., "password": ...}`. Requires the
optional `asyncpg` package at connection-build time.
