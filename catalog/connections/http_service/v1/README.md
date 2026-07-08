# http_service (v1 — api_key)

Generic HTTP JSON service. The binding's `config.base_url` fixes where
requests go; credentials inject an API-key header (template configurable).

## Binding example

```yaml
connections:
  my_service:
    ref: catalog/http_service
    version: v1
    config:
      base_url: https://api.example-corp.test
      health_path: /health
    credentials_ref: { kind: env, value: MY_SERVICE_API_KEY }
```

Empty credentials (kind: default) build an unauthenticated client — useful
for public APIs and local test doubles.

## Refresh

`refresh.mode: on_auth_error` — a tool raising ConnectionAuthError evicts
the pool entry and the call is retried once against a rebuilt client.
