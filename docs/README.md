# agent-foundry — design docs

This directory is the source of truth for the design of `agent-foundry`, a personal developer kit for building, evaluating, versioning, and orchestrating multi-agent systems. The docs are organised in tiers; each tier is a conceptual layer of the system and each file in a tier is a deep spec for one component of that layer.

## How to read these docs

Read Tier 0 first — it frames everything else.

| Order | Path | Purpose |
|---|---|---|
| 1 | `00-vision-and-scope.md` | What this is, why it exists, non-goals, guiding principles. |
| 2 | `02-framework-evaluation.md` | Framework decision (LangGraph). Informs every layer below. |
| 3 | `01-architecture-overview.md` | Whole system on one page: layers, lifecycle, primitives, boundaries. |
| 4 | `03-development-phases.md` | Phased roadmap with exit criteria. |

After Tier 0 you can either read top-down (Tier 1 → Tier 8) or jump to a specific layer:

| Tier | Layer | Docs |
|---|---|---|
| 1 | Core framework | `10-core-framework.md` · `11-provider-abstraction.md` · `12-config-and-validation.md` |
| 2 | Agents, tools, state, connections, caching, retrieval | `20-tool-system.md` · `21-agent-system.md` · `22-state-management.md` · `23-connections-and-auth.md` · `24-caching-and-optimisation.md` · `25-retrieval-and-rag.md` |
| 3 | Orchestration | `30-orchestration-patterns.md` · `31-multi-agent-systems.md` · `32-human-in-the-loop.md` |
| 4 | Eval | `40-eval-harness.md` · `41-eval-driven-iteration.md` |
| 5 | Versioning & rollback | `50-versioning-model.md` · `51-git-backbone.md` · `52-rollback-and-audit.md` |
| 6 | Meta-agent | `60-meta-agent.md` · `61-meta-tools.md` · `62-configurator-sessions.md` |
| 7 | API & async runtime | `70-api-layer.md` · `71-async-runtime.md` |
| 8 | Observability, storage, dev UX, security, deploy, batch & throughput, multi-tenancy & IP | `80-observability.md` · `81-storage-and-artifacts.md` · `82-dev-ux.md` · `83-security-guardrails.md` · `84-deployment.md` · `85-batch-and-throughput.md` · `86-multi-tenancy-and-ip.md` |

## Doc conventions

- Each spec doc has these sections: **Purpose · Inputs/Outputs · Public interfaces · Schemas · Invariants · Failure modes · Implementation notes · Test expectations · Open questions**. Not every doc uses every section, but the shape is consistent.
- Pydantic schemas are shown as Python code blocks. They are normative — implementations must match the field names and types exactly.
- Directory paths are always project-relative from `/Users/sam/projects/agent-foundry/`.
- Code samples are illustrative signatures, not final implementations. Final implementations may refine them but must preserve the contracts stated in **Invariants**.
- "MUST / MUST NOT / SHOULD" are used in the RFC-2119 sense when contracts are load-bearing.

## Relationship to the seed idea

`personal_docs/meta-agent-configurator.jsx` describes a narrow, ~200-line meta-agent that edits YAML/markdown configs. That is the **seed**. This doc tree expands it into a full developer kit. The seed's core claim — *configs are text, LLMs edit text, agents are config-driven* — is preserved as a foundational principle throughout (see `00-vision-and-scope.md` § Guiding principles).
