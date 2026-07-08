# http_service (v2 — basic_auth)

Same service shape as v1; auth scheme swapped to HTTP basic. Credentials
must resolve to a JSON object: `{"username": "...", "password": "..."}`.

Pin bump `v1 -> v2` in system.yaml is the whole migration for consuming
tools (docs/23 § Versioning connections).
