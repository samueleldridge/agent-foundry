# `projects/` — configured multi-agent systems

`projects/` is the third top-level tree (alongside `src/foundry/` and
`catalog/`). It holds **institution-specific configured systems** — each
project is a directory containing a `SystemSpec`, agent configs, prompts,
local tools, local connections, and evals.

## Status

Three example projects ship with Phase 2: `hello` (Phase 1/2a — one agent,
one catalog tool through one catalog connection), `rag_hello` (Phase 2b —
hybrid retrieval + rerank + semantic cache), and `memory_hello` (Phase 2c —
three-layer memory + FunctionNodes in a sequential flow).

## What lives here

When populated, each project follows this shape (per
`docs/01-architecture-overview.md` § Directory layout):

```
projects/<project_name>/
├── system.yaml             SystemSpec — pins tool + connection versions
├── state.yaml              StateSpec + per-node visibility
├── agents/<agent>/
│   ├── agent.yaml          AgentSpec — pins prompt version
│   ├── prompts/v<N>.md     versioned prompts
│   └── output_schema.py    Pydantic output model
├── tools/<tool>/v<N>/...   project-local tools (not in catalog)
├── connections/<name>/v<N>/...  project-local connections
├── evals/<name>.yaml       end-to-end project evals
└── .foundry/               per-project audit log and run index
```

## Access rules (enforced; not just conventional)

- **Meta-agent has full WRITE access here, scoped per session.** The
  configurator's sandbox is rooted at a single `projects/<name>/` per session;
  it cannot cross-write between projects or escape upward.
- **In multi-institution deployments**, `projects/` lives in the institution's
  private repo, not in the upstream framework repo. The runtime locates
  projects via `FOUNDRY_PROJECTS_ROOT`. See
  `docs/86-multi-tenancy-and-ip.md`.
- **Branch model**: per-project work happens on `foundry/<project_name>`
  branches once projects exist (see `docs/51-git-backbone.md`).
- **Secrets never live here.** Credentials are resolved at runtime via a
  `SecretsProvider`; the config-load secret-literal scan rejects any value
  that looks like a credential.

See `docs/31-multi-agent-systems.md` for the `SystemSpec` shape and
`docs/50-versioning-model.md` for how rollback / promotion compose across
this tree.
