# http_get_json

Authenticated GET against the bound `service` connection; returns parsed
JSON (or raw text when the body is not JSON).

## When to use
- Reading from a typed REST endpoint the project has bound as a
  `catalog/http_service` connection (status endpoints, public data APIs).

## When NOT to use
- Arbitrary URL fetching — the base URL is fixed by the connection on
  purpose. A generic fetcher would be a `dangerous: true` tool.
- Writes (POST/PUT) — read-only by design.

## Connections required
- `service` — a `catalog/http_service` connection. v1 of the connection
  injects an API key header; v2 uses basic auth.

## Edge cases
- 401/403 raise ConnectionAuthError so `refresh.mode: on_auth_error`
  connections evict + rebuild once before failing.
- Non-JSON bodies are returned as a raw string in `json_body`.
