# postgres (v1)

PostgreSQL via an asyncpg pool. Credentials JSON: `{"username": ...,
"password": ...}`. Bind read-only roles for query tools (least privilege).

Requires the optional `asyncpg` package at connection-build time.
