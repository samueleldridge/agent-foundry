# `catalog/` — the shared, versioned artifact library

`catalog/` is one of the three top-level trees the foundry cares about
(alongside `src/foundry/` and `projects/`). It holds **generic, reusable
artifacts** — tools, connections, retrievers, and agent templates — that any
project can pin by version.

## Phase 0 status

Empty placeholder. Seed artifacts (`http_get`, `query_postgres`, `postgres`
connection, etc.) land in Phase 2a. Catalog index loading is implemented in
Phase 2a; human-gated promotion is implemented in Phase 5.

## What lives here

When populated, the layout is (per `docs/01-architecture-overview.md`
§ Directory layout):

```
catalog/
└── public/
    ├── tools/<name>/v<N>/{tool.yaml, handler.py, schemas.py, eval.yaml, README.md}
    ├── connections/<name>/v<N>/{connection.yaml, auth.py, schemas.py, health.yaml, README.md}
    ├── retrievers/<name>/v<N>/...
    ├── agent_templates/<name>/v<N>/...
    └── index.yaml
```

## Access rules (enforced; not just conventional)

- **The meta-agent READS but does not WRITE.** The configurator's `write_file`
  sandbox refuses any path under `catalog/`. Promotion is a deliberate human
  action via `foundry catalog promote`, gated on the artifact's eval score.
- **Institution-specific artifacts do not belong here.** Anything firm- or
  client-specific lives in a private overlay catalog mounted via
  `FOUNDRY_CATALOG_ROOTS`. See `docs/01` § Multi-institution deployment
  pattern and `docs/86-multi-tenancy-and-ip.md`.
- **Every artifact is directory-versioned.** Existing version directories are
  immutable once committed; promotion creates a new `vN+1/` directory.

See `docs/20-tool-system.md`, `docs/23-connections-and-auth.md`, and
`docs/50-versioning-model.md` for the per-artifact specs.
