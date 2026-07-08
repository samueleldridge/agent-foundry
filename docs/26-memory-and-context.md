# 26 — Memory and Context

## Purpose

This doc specifies the foundry's **multi-layer memory** subsystem: a coordinator that gathers context from one or more `MemoryLayer`s, assembles them into a typed envelope, weaves them into the agent's prompt, and routes writes back. Three standard layer kinds are shipped — **working** (recency window over conversation), **episodic** (vector retrieval over past conversation history), and **semantic** (synthesised summaries / facts / persona content) — each implemented on top of primitives we already have.

Cross-session persistent memory (a `MemoryStore` keyed by `user_id` / `session_id` for multi-visit recall) is **explicitly deferred** to v1.1. The protocol shape doesn't preclude it; a future `PersistentSemanticLayer` slots in via the same `MemoryLayer` interface.

The lightweight primitive earns its keep because:
- Customer-facing agents (when they arrive) get a clean, configurable axis.
- Batch / one-shot agents pay nothing — `memory: None` is the default.
- Memory becomes an explicit eval axis (`compare_versions` can vary it).
- Multi-layer memory has a single shared vocabulary across projects.

## Mental model

```
            ┌───────── agent step begins ─────────┐
            │                                     │
            │  ┌────────────────────────────────┐ │
            │  │  Memory.read(query, ctx)       │ │
            │  │                                │ │
            │  │  for layer in layers:          │ │
            │  │    contribution = layer.read() │ │
            │  │    (degrade on failure)        │ │
            │  │                                │ │
            │  │  envelope = MemoryEnvelope(    │ │
            │  │    contributions=[             │ │
            │  │      WorkingContribution,      │ │
            │  │      EpisodicContribution,     │ │
            │  │      SemanticContribution,     │ │
            │  │    ])                          │ │
            │  └──────────────┬─────────────────┘ │
            │                 │                   │
            │                 ▼                   │
            │  ┌────────────────────────────────┐ │
            │  │  prompt_assembly.weave(        │ │
            │  │    envelope, injection_rules)  │ │
            │  │                                │ │
            │  │  → final messages sent to LLM  │ │
            │  └──────────────┬─────────────────┘ │
            │                 │                   │
            │                 ▼                   │
            │  Provider.generate(messages, ...)   │
            │                 │                   │
            │                 ▼                   │
            │  agent output, state update         │
            │                 │                   │
            │  ┌──────────────▼──────────────┐    │
            │  │  Memory.write(...)          │    │
            │  │  (e.g., new message → working)│  │
            │  └─────────────────────────────┘    │
            │                                     │
            │  if turn_count % consolidate_n == 0 │
            │  ┌─────────────────────────────┐    │
            │  │  Memory.consolidate(ctx)    │    │
            │  │  → semantic layer summarise │    │
            │  └─────────────────────────────┘    │
            │                                     │
            └─────────────────────────────────────┘
```

The orchestration layer wires `Memory.read` before the LLM call and `Memory.write` / `consolidate` after, based on agent config. The `Session` carries a `MemoryAccessor` (parallel to `CacheAccessor`); when `memory: None`, the accessor is a NoOp and zero overhead is added.

## Layer kinds

### Working memory

**Purpose**: keep the most recent conversation in the prompt verbatim.

**Mechanism**: reads from a state field (default `messages`), trims to a configured window (`max_messages` OR `max_tokens`).

**Implementation**: stateless. Nothing is written to a separate store — the state field is the source of truth, and the layer projects a windowed view at read time.

**Typical injection**: `placement: messages` — the layer's contribution literally *is* the conversation messages the LLM sees.

**Typical config**:

```yaml
- kind: working
  name: short_term
  source_field: messages
  window:
    max_messages: 20
```

### Episodic memory

**Purpose**: surface past conversation snippets relevant to the current query, even when they're outside the working window.

**Mechanism**: wraps a `Retriever` (`25-retrieval-and-rag.md`) bound to a "conversation history" corpus. Past turns are embedded and stored at write time; lookup at read time uses the current query.

**Implementation**: read = retrieval call; write = ingestion into the corpus (hand-off to the retriever's underlying connection, e.g. `pgvector_dense` upserts a chunk).

**Typical injection**: `placement: system_suffix` with a template like:

```
Relevant past context:
{docs}
```

**Typical config**:

```yaml
- kind: episodic
  name: past_conversations
  retriever_slot: conversation_history
  top_k: 5
  relevance_threshold: 0.7
```

`retriever_slot` must be defined in `agent.retrievers` — the same retriever-binding pattern that RAG uses. There is no separate "episodic store" primitive; episodic memory IS retrieval over conversation snippets.

### Semantic memory

**Purpose**: hold synthesised content — summaries, facts the agent has learned, persona / "soul.md" sections, user preferences. Content is a single small artefact (typically Markdown, often <2k tokens) that the agent reads on every turn and an LLM consolidator periodically refreshes.

**Mechanism**: reads a state field; consolidation runs an LLM call that takes recent activity + the prior synthesised content and produces an updated synthesis, written back to the state field.

**Implementation**: read = state field projection; consolidate = LLM call via the agent's main `model_binding` or a separate `consolidator_model_binding` (typically a cheaper model like Haiku). Trigger options: every N turns, on session end, or explicit (a tool call or lifecycle hook fires it).

**Typical injection**: `placement: system_prefix` with a template like:

```
What you've learned about this user:
{content}
```

**Typical config**:

```yaml
- kind: semantic
  name: user_facts
  state_field: synthesised_facts
  consolidate_every_n_turns: 10
  consolidator_prompt: prompts/consolidate_facts_v1.md
  consolidator_model_binding:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  max_size_tokens: 1500
```

The consolidator prompt receives:
- The current synthesised content (`{current}`)
- Recent activity since last consolidation (`{recent_messages}`)
- The schema for output (`{schema}` — typically Markdown with a stable structure)

It must return updated content within `max_size_tokens`. The state-field write happens transactionally with the consolidate event emission.

### Custom layers

The `MemoryLayer` protocol is `kind: "custom"`-aware. Users can implement arbitrary layers — e.g. structured-fact graphs, vector summarised episodes — by implementing the three protocol methods. Catalog templates encouraged once a pattern proves useful across projects.

## Configuration

### `MemoryConfig` on `AgentSpec`

Specified in `12-config-and-validation.md`. Recap of the shape:

```yaml
memory:
  layers:
    - kind: working
      name: short_term
      source_field: messages
      window:
        max_messages: 20
    - kind: episodic
      name: past_conversations
      retriever_slot: conversation_history
      top_k: 5
      relevance_threshold: 0.7
    - kind: semantic
      name: user_facts
      state_field: synthesised_facts
      consolidate_every_n_turns: 10
      consolidator_prompt: prompts/consolidate_facts_v1.md
      max_size_tokens: 1500
  inject_into_prompt:
    - layer: short_term
      placement: messages
    - layer: past_conversations
      placement: system_suffix
      template: |
        Relevant past context:
        {docs}
    - layer: user_facts
      placement: system_prefix
      template: |
        What you've learned about this user:
        {content}
  max_envelope_tokens: 8000
  fail_strict: false
```

Defaults if `inject_into_prompt` is omitted:

| Layer kind | Default placement | Default template |
|---|---|---|
| working | `messages` | (no template — content IS the messages) |
| episodic | `system_suffix` | `"Relevant past context:\n{docs}"` |
| semantic | `system_prefix` | `"Persistent context:\n{content}"` |

Custom layers must always specify both `placement` and `template`.

### Validation rules (compile-time)

- Layer names unique.
- Working layer's `source_field` exists in `StateSpec.schema` and is a list of `FoundryMessage` or a string.
- Semantic layer's `state_field` exists in `StateSpec.schema` and the agent declares write access to it via `state_visibility.write`.
- Episodic layer's `retriever_slot` is bound in `agent.retrievers`.
- Semantic layer with at least one consolidation trigger requires `consolidator_prompt` to exist on disk.
- `max_envelope_tokens` is sensible (≥ sum of layer-level `max_tokens` would be a tightness warning, not error).
- Injection rule `layer` references a name in `MemoryConfig.layers`.

Failures at any of these → `MemoryConfigError` with the bad field's path.

> **Errata (Phase 2c):** the implementation splits the compile-time failures above into two classes — field/type/file issues (state-field existence, working `source_field` type, consolidator prompt on disk) raise `MemoryConfigError`; scope/slot issues (a layer reading/writing outside the agent's `state_visibility`, an unbound `retriever_slot`) raise `CompileError`, matching docs/03 § Phase 2c. Both are load-time.

## Lifecycle

### Per-turn

```
agent_step:
    memory_envelope = await session.memory.read(query, ctx)
    prompt = assemble_prompt(agent.system_prompt, memory_envelope, injection_rules)
    response = await provider.generate(prompt + new_user_message, tools, settings)
    
    state_delta = process_response(response)
    
    # Memory writes happen as part of state-application
    if memory_config.has_writable_layers:
        for write in memory_writes_from_delta(state_delta):
            await session.memory.write(write, ctx)
    
    # Periodic consolidation
    if should_consolidate(turn_count, session_state):
        await session.memory.consolidate(ctx)
```

### Cross-turn

- Working memory: mutates implicitly via state's `messages` field reducer.
- Episodic memory: each completed message can be optionally ingested into the conversation-history retriever's corpus (configurable per layer; default is "ingest after the agent responds").
- Semantic memory: consolidated on configured triggers; the most recent synthesis is what the next turn's `read` returns.

### Session end

- For agents with explicit session boundaries (e.g. an API call that closes a chat), `consolidate_on_session_end: true` triggers a final consolidation pass before the session is checkpointed and closed.
- For batch / one-shot agents, this is moot — they don't have persistent sessions.

## Prompt assembly

The `prompt_assembly.weave()` function takes the envelope and produces the final list of `FoundryMessage`s sent to the provider. Rules:

1. Layers contribute in the order declared in `MemoryConfig.layers`. Injection rules can reorder by listing them differently in `inject_into_prompt`.
2. `placement: messages` contributes raw message-list content into the conversation. Multiple layers with `placement: messages` concatenate in declared order (rare; usually only one).
3. `placement: system_prefix` / `system_suffix` wrap text into the system message — prefix renders before the agent's hand-authored system prompt, suffix renders after.
4. `placement: user_message_prefix` injects content as a typed boundary block at the start of the latest user message.
5. Templates use `{content}` (semantic / custom) or `{docs}` (episodic) or `{messages}` (custom) — the carrier matches the layer's `MemoryContribution.content` type.
6. Per-rule `max_tokens` truncates that layer's contribution. Envelope-level `max_envelope_tokens` is a final safety net; if exceeded, the LAST listed layer's contribution truncates first.

### Tool-output boundary preservation

If `placement: user_message_prefix` is used, the rendered text is wrapped in a typed boundary (`<memory layer="..." kind="...">...</memory>`) so prompt-injection guardrails (per `83-security-guardrails.md`) can detect cross-boundary injection attempts originating from semantic memory content.

## Failure modes

| Cause | Default behaviour (`fail_strict: false`) | Strict (`fail_strict: true`) |
|---|---|---|
| Episodic retriever unavailable | empty contribution + warning event | `MemoryLayerError` |
| Semantic state field missing | `MemoryConfigError` at compile (always strict) | same |
| Consolidator LLM call fails | `MemoryConsolidateError`; existing synthesis preserved; warning event; run continues with stale content | `MemoryConsolidateError` raised |
| Working memory state field empty | empty messages contribution | empty messages contribution (strict makes no difference here) |
| Total envelope exceeds `max_envelope_tokens` | truncate last-listed layers; `truncated: true` flag | same — truncation isn't a failure |
| Custom layer raises uncaught | empty contribution + warning event | `MemoryLayerError` |

**Fail-safe default**: every layer error degrades gracefully. The agent still gets a prompt (possibly with empty layer contributions). The audit trail flags every degradation. Strict mode is for cases where memory failure means the run is meaningless.

## Observability

Three event types (defined in `10-core-framework.md`):

- **`memory.read`** — every turn that has memory enabled. Attributes: `layers_read`, `layers_failed`, `total_tokens_estimate`, `truncated`.
- **`memory.write`** — every successful write to a writable layer. Attributes: `layer_name`, `layer_kind`, `write_kind`, `bytes`.
- **`memory.consolidate`** — every consolidation run. Attributes: `layer_name`, `trigger`, `input_tokens_summarised`, `output_tokens_written`, `latency_ms`.

Derived metrics surface in `foundry obs`:

- `foundry.memory.read.tokens` — distribution of envelope sizes (catch context bloat).
- `foundry.memory.consolidate.cost_usd` — cumulative consolidator spend.
- `foundry.memory.layer_failure_rate` — by layer, by week. Alert if a layer becomes unreliable.

A debug query: "for run X, what was in the agent's memory envelope?" — answered by reading the `memory.read` event + (with `capture_inputs: true`) the assembled prompt that followed.

## Composition with other primitives

| Memory layer | Underpinned by | Notes |
|---|---|---|
| Working | `StateBase` field + reducer (likely `APPEND` for messages) | Window applied at read time; no extra storage. |
| Episodic | `Retriever` (`25-retrieval-and-rag.md`) | Conversation history is just another retrieval corpus; ingestion typically happens via a hook that upserts after each turn. |
| Semantic | `StateBase` field + LLM call via `Provider` | Consolidator prompt is a versioned prompt under the agent dir. |

Memory is observable, configurable, swappable. The coordinator does the hard composition once; agents declare what they want.

## Cross-session memory: deferred design vector

The current design covers all *intra-run* memory (within a single run / session). Cross-session memory (remembering across visits, days, weeks) requires:

1. A **`MemoryStore`** primitive — keyed on `user_id` (or `session_id`), with retention + privacy controls.
2. A **`PersistentSemanticLayer`** — same `MemoryLayer` protocol, but the state field is materialised from the `MemoryStore` on session start and synced back on session end.
3. Session-identity threading — `Session` gains a `user_id` field; the API layer's `POST /run` accepts user identity.

None of this is in v1. Sketch only:

```python
# Future v1.1 (not built yet):
class MemoryStore(Protocol):
    async def load(self, key: str) -> dict[str, Any]: ...
    async def save(self, key: str, content: dict[str, Any]) -> None: ...
    async def list_keys(self, prefix: str) -> list[str]: ...
```

Backings: Postgres (one row per user-agent pair), Redis (TTL-based), or external (e.g., Letta, Mem0, Zep). Foundry would ship adapters; users supply credentials and retention policy via existing `SecretsProvider` pattern.

When this lands, existing semantic memory configs gain an opt-in:

```yaml
- kind: semantic
  name: user_facts
  state_field: synthesised_facts
  persistent: true                        # NEW in v1.1
  store_ref: catalog/postgres_memory_store@v1
  store_key_template: "agent:{agent_name}:user:{user_id}"
```

The protocol shape doesn't change; one additional configuration knob slots in. Existing v1 memory configs continue to work unchanged.

## Persona / "soul.md" — explicit non-feature

A persona is just a prompt section. The foundry already has versioned prompts (`prompts/v<N>.md`). A consistent persona is `# Persona\n\n...` at the top of the agent's prompt; it's already version-tracked, eval-comparable, rollback-able.

Wrapping persona as a separate "soul" artifact would add ceremony without earning its keep, *unless* the persona needs to evolve at runtime — at which point it's a semantic memory layer with the persona content as `state_field`, and the consolidator updates it on configured triggers. Already supported.

If three+ projects ask for a `persona` artifact category in the catalog, revisit. For now: prompt section.

## Invariants

1. **Memory is off by default.** `AgentSpec.memory: MemoryConfig | None = None`. Existing batch / one-shot agents are unaffected.
2. **Layer names are unique within an agent.** Compile-validated.
3. **`Memory.read` always succeeds in default (non-strict) mode.** Layer failures degrade to empty contributions + warning events, never abort the run.
4. **Consolidator failures preserve prior synthesis.** Stale synthesised content is better than no synthesised content; refresh fails open.
5. **Working memory is read-only against state.** It does not duplicate or mutate the messages field; it projects a windowed view at read time.
6. **Episodic memory uses an existing `Retriever`.** No separate "episodic store" primitive — conversation history is just another retrieval corpus.
7. **Every memory operation emits a typed event.** No silent reads / writes / consolidations in the audit trail.
8. **Cross-session persistence is not in v1.** Documented; deferred without breaking the protocol.

## Test expectations

### Unit

1. **Working layer windowing**: state with 50 messages, `max_messages: 5` → contribution has exactly the last 5; same with `max_tokens` enforced via tokeniser.
2. **Episodic layer**: configured retriever returns 10 docs; layer respects `top_k: 5` and `relevance_threshold: 0.7`.
3. **Semantic layer read**: state field `synthesised_facts: "..."` → contribution carries that text.
4. **Semantic consolidator**: triggered with synthesised input messages, calls the consolidator model, writes returned content to the state field, emits `memory.consolidate` with token counts.
5. **Layer name uniqueness validator** at config load.
6. **Envelope truncation**: contributions over `max_envelope_tokens` → last-listed truncated, `truncated: true`.
7. **Degrade on failure**: episodic retriever raises → contribution is empty, `layers_failed` includes its name, run continues.
8. **Strict mode**: same setup with `fail_strict: true` → `MemoryLayerError` raised.

### Integration (Phase 2 exit gate, in addition to existing)

1. End-to-end: an agent with all three layers configured, run a 12-turn conversation; assert prompts at turns 1, 5, 11 reflect the configured envelope (working window grows then trims; semantic content updates after consolidation at turn 10; episodic retrieves relevant past).
2. Memory observability: every turn emits `memory.read`; expected layer-specific writes and one `memory.consolidate` at turn 10.
3. Same agent with `memory: null` runs identically except no memory events; baseline cost / latency comparable.

## CLI surface (previewed)

- `foundry obs memory <project> --agent <name>` — recent memory event summary; envelope sizes, consolidation cadence, failure rates.
- `foundry agent inspect-memory <project>/<agent>` — print the *current* state-field content for semantic layers and the working-window slice (debug aid).
- `foundry agent consolidate <project>/<agent> --layer <name>` — manual trigger for a semantic-layer consolidator (operations / dev tooling).

## Open questions

1. **Default consolidator prompt template.** Should the foundry ship a generic consolidator prompt under `catalog/agent_templates/consolidators/`? Lean: yes, ship one or two (general + customer-facing) so projects don't all reinvent. Catalog-side, not framework primitive.
2. **Layered eval comparison.** `compare_versions` natively varies prompts, tools, models; should it natively vary memory configs? Lean: yes — small extension to the eval comparator. Worth doing in Phase 4.
3. **Memory writes from the LLM.** Should the LLM be able to call a `remember(content)` tool that explicitly writes to a semantic layer? Lean: yes, optional; ship as a catalog tool template that any agent with memory can wire. Easy to add; meaningful UX.
4. **Inter-agent memory sharing.** In a multi-agent system, can supervisor and workers share semantic memory? Currently no — memory is per-agent. A "shared semantic field" mode (project-scoped) would be additive; revisit if real use cases surface.
5. **Compression of working memory itself.** Beyond simple windowing, should working memory support "summarise older messages into a compact synopsis at the front of the window" without full semantic-layer ceremony? Probably yes as a new layer kind `compressed_working` — but defer until requested.
