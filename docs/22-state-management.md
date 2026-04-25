# 22 — State Management

## Purpose

State is the shared blackboard that agents in a project read from and write to. This doc specifies the full state model: schema declaration, field types, reducer semantics, per-agent visibility, compile-time enforcement, the message-history convention, integration with memory and checkpointing, and how state shape compiles down to a LangGraph runtime state.

The `StateBase` and `Reducer` primitives are in `10-core-framework.md`. The `StateSpec` schema is in `12-config-and-validation.md`. Memory layers consume state fields per `26-memory-and-context.md`. This doc is the consolidating spec.

Three load-bearing properties:

1. **State is Pydantic-typed end to end.** No untyped dicts cross the runtime; the Pydantic model derived from `StateSpec.schema` is the source of truth.
2. **Visibility is configuration, not code.** Each agent declares `read` and `write` lists; the orchestration compiler enforces them via subgraph schemas. A worker cannot read fields it doesn't have visibility for — they're literally projected out of its input.
3. **Reducers determine merge semantics.** Concurrent writes from parallel agent nodes are not last-write-wins by default; per-field reducer metadata governs merging.

## What state IS

| State is | State is not |
|---|---|
| A typed Pydantic model with reducer metadata | A free-form dict |
| Per-project (single `state.yaml` per project) | Per-agent or per-tool |
| Mutable by agents within their declared `write` scope | Mutable by tools (tools return outputs; agents apply them) |
| Persisted by the checkpointer at every node boundary | Per-LLM-call ephemeral (only the agent's projection of state is fed to the LLM) |
| The single coordination point between agents in a multi-agent system | The transport for inter-process communication |

## Module layout

```
src/foundry/core/
└── state.py             StateBase, Reducer enum, reducer metadata helpers

src/foundry/orchestration/
└── state_scope.py       compile-time visibility validation; StateVisibilityError generation
└── state_compiler.py    StateSpec → Pydantic model → LangGraph TypedDict + reducer dict

projects/<name>/
└── state.yaml           StateSpec — the project's state shape + visibility + reducer overrides
```

## `state.yaml`

Single file per project. Defines the state schema, reducer overrides, and per-agent visibility rules. Full Pydantic schema in `12-config-and-validation.md`. Recap with realistic example:

```yaml
schema:
  messages:
    type: list[FoundryMessage]
    description: Conversation messages across the run
  current_exception:
    type: Exception
    description: Inbound exception record being investigated this run
  detected_breaks:
    type: list[DetectedBreak]
    description: All breaks the detector classified
  investigation_result:
    type: Investigation
    default: null
    description: Final investigation output (populated by investigator agent)
  scratchpad:
    type: dict[str, Any]
    description: Free-form working area for agents (use sparingly)
  cost_so_far_usd:
    type: Decimal
    default: "0"
    description: Running total of LLM spend; updated by the runtime

reducers:
  messages: append
  detected_breaks: append
  scratchpad: merge
  cost_so_far_usd: last_write_wins   # explicit even though it's the default

visibility:
  break_detector:
    read: [current_exception]
    write: [detected_breaks]

  investigator:
    read: [messages, current_exception, detected_breaks]
    write: [messages, investigation_result]

  resolver:
    read: [messages, investigation_result]
    write: [messages]

schema_version: 1
```

The visibility section MUST cover every agent named in `SystemSpec.agents` — missing an agent is a compile-time `StateVisibilityError`.

## Field types and the type system

Each field in `schema:` declares a `type` string parsed by `state_compiler.py` into a concrete Python type. Supported:

### Primitive types

```
str
int
float
bool
bytes
datetime
date
time
timedelta
Decimal
UUID
```

### Container types

```
list[<T>]                  # ordered, append by default
dict[<K>, <V>]             # K is str typically; merge by default
set[<T>]                   # unique, merge-as-union semantics
tuple[<T>, ...]            # immutable
```

Where `<T>` is any supported type.

### Optional types

```
<T> | None                 # equivalent to Optional[T]; default None unless specified
```

### Foundry types

```
FoundryMessage             # from foundry.core.messages
RetrievedDocument          # from foundry.core.retrieval
ConnectionDescriptor       # rare in state, but allowed (e.g., debugging captures)
```

### User Pydantic types

```
BaseModel:<module>:<ClassName>
```

E.g. `BaseModel:projects.pipeline_recon.types:Exception` references a class importable from the project's Python path. The compiler resolves at compile time; missing class → `ConfigError`.

### Forward references

Types may reference each other cyclically (e.g., `Investigation` containing a `list[EvidenceItem]` where `EvidenceItem` is also in state). The compiler handles via Pydantic's `model_rebuild()` after all types are loaded.

### Discriminated unions

For state fields that hold tagged-union values:

```yaml
detected_breaks:
  type: list[BaseModel:types:Break]
```

Where `Break` itself is a `Annotated[Union[...], Field(discriminator="type")]`. The state compiler doesn't special-case this — it works because Pydantic supports it natively.

### What's NOT supported

- Arbitrary classes that aren't Pydantic models (use a Pydantic wrapper).
- Functions / callables.
- Generators / iterators.
- File handles / sockets / connections (these belong in `RunContext`, not state).

The reasoning: state must be JSON-serialisable for checkpointing. Pydantic enforces this; bypassing Pydantic risks state that can't be persisted, which means runs can't resume.

## Reducers

Reducers govern how concurrent or sequential writes to the same field combine. Defined in `10-core-framework.md`:

```python
class Reducer(StrEnum):
    APPEND = "append"
    MERGE = "merge"
    LAST_WRITE_WINS = "last_write_wins"
    REPLACE_IF_SET = "replace_if_set"
```

### `APPEND`

For lists. Concatenates writes preserving order. Two parallel agents writing `messages: [m1]` and `messages: [m2]` produce `messages: [..., m1, m2]` in serialisation order (LangGraph determines order from node-completion order).

```yaml
reducers:
  messages: append
  detected_breaks: append
```

### `MERGE`

For dicts. Shallow-merges keys; later writes overwrite same-key earlier writes within the same step. Useful for namespaced scratchpads:

```yaml
scratchpad:
  type: dict[str, Any]
reducers:
  scratchpad: merge
```

Two parallel agents writing `scratchpad: {"a": 1}` and `scratchpad: {"b": 2}` produce `scratchpad: {"a": 1, "b": 2}`. Both writing `{"a": 1}` and `{"a": 2}` produces `{"a": <serialisation-order-winner>}`.

For sets, `MERGE` performs set union.

### `LAST_WRITE_WINS`

Default for unannotated fields. The most recent write replaces the existing value. Simple; appropriate for status fields, single result fields, counters.

```yaml
investigation_result:
  type: Investigation
  default: null
reducers:
  investigation_result: last_write_wins   # explicit; same as default
```

### `REPLACE_IF_SET`

Variant of LAST_WRITE_WINS with one rule: a `None` write does NOT overwrite an existing non-None value. Useful for "first writer wins" or "preserve once populated" patterns.

```yaml
final_decision:
  type: Decision | None
  default: null
reducers:
  final_decision: replace_if_set
```

A worker that returns `final_decision: None` doesn't clobber a sibling's earlier `final_decision: <real value>`.

### Default reducer

Fields not listed in the `reducers:` block default to `LAST_WRITE_WINS`. Explicit declaration is recommended for documentation clarity even when the default suffices.

### Custom reducers

Locked decision (per `00-vision-and-scope.md`): NOT supported in v1. The four enum values cover the dominant patterns; complex consolidation logic (e.g. "keep top-10 by score") is implementable as a dedicated agent that reads, processes, and writes back via standard LWW semantics. Revisit if real use cases demand.

## Per-agent visibility

Visibility is the central safety property of multi-agent foundry projects. Every agent declares:

```yaml
visibility:
  <agent_name>:
    read: [field1, field2, ...]
    write: [field1, field3, ...]
```

### Compile-time validation

The state compiler walks `SystemSpec.agents`; for each agent, validates:

1. The agent has a visibility entry. Missing → `StateVisibilityError("agent X has no visibility entry")`.
2. Every field in `read` exists in `schema:`. Missing → `StateVisibilityError("agent X reads unknown field Y")`.
3. Every field in `write` exists in `schema:`. Missing → same error pattern.
4. Empty `read` AND empty `write` → `StateVisibilityError("agent X must declare at least one of read or write")`.

Compile fails fast with the field path + suggested fix.

### Runtime enforcement

The compiler generates per-agent `TypedDict` schemas:

```python
# Generated for break_detector agent:
class BreakDetectorInput(TypedDict):
    current_exception: Exception
    # `messages`, `detected_breaks`, `investigation_result`, etc. are NOT here

class BreakDetectorOutput(TypedDict):
    detected_breaks: list[DetectedBreak]
```

The agent's LangGraph node accepts `BreakDetectorInput` and returns `BreakDetectorOutput`. The runtime projects full state down to the input schema before invoking; merges output back to full state per reducers.

This is a structural enforcement: the agent literally cannot reference a field outside its visibility because the field is not in its scope. No runtime checks needed — the projection is by construction.

### Visibility patterns

| Pattern | Read | Write | Example |
|---|---|---|---|
| Pure consumer | many fields | (one or few output fields) | Investigator: reads context, writes investigation_result |
| Pure producer | (one trigger) | many fields | Detector: reads current_exception, writes detected_breaks + scratchpad |
| Coordinator | most fields | most fields | Supervisor: needs visibility into the whole pipeline |
| Logger / observer | many | none | Audit agent that summarises state without mutating |

The principle of least visibility applies: declare only what the agent demonstrably needs. Tighter visibility means clearer boundaries, easier reasoning, and better isolation in eval.

### Cross-agent communication via state

Agents communicate by reading and writing state fields, NOT by direct calls. A supervisor that wants a worker's output:

1. Worker writes `worker_result: <value>` (per its `write` visibility).
2. Reducer merges into state.
3. Supervisor (in next node) reads `worker_result` (per its `read` visibility).

This is the LangGraph-native pattern; the foundry's state visibility is a configuration layer above it.

## Message-history convention

Most agents need access to a conversation message history. The convention:

```yaml
schema:
  messages:
    type: list[FoundryMessage]
    description: Run-wide conversation history

reducers:
  messages: append
```

Every agent that reads conversation appends `FoundryMessage` objects to this field. Memory's `WorkingMemoryLayer` reads from `messages` by default. Tools that emit `tool_result` blocks contribute messages via the agent's standard message-construction path.

Variations:

- **Per-agent messages**: a multi-agent system where each agent has its own message stream uses `<agent>_messages` fields per agent. Useful when conversations diverge.
- **Compressed messages**: a `summarised_messages: str` field (single string) that semantic memory writes to; working memory reads the latest detail messages, semantic memory pulls the summary.
- **No message history**: batch / tools-free agents may have no `messages` field at all; their state is task-structured.

The framework doesn't mandate `messages`. It's a convention that most conversational agents follow because LLM provider message formats expect it.

## State + memory layer integration

Memory layers (`26-memory-and-context.md`) read and write specific state fields:

- **`WorkingMemoryLayer.source_field`** — typically `messages`. Layer projects a windowed view at read time; never mutates the field.
- **`SemanticMemoryLayer.state_field`** — typically a free string like `synthesised_facts: str`. Layer reads on every turn; consolidator writes periodically.
- **`EpisodicMemoryLayer`** — writes go to a *retriever's backing store*, not a state field. State doesn't hold episodic content directly.

Compile-time validation:
- Memory layer's `source_field` / `state_field` MUST exist in `StateSpec.schema`.
- For `SemanticMemoryLayer`, the agent's `state_visibility.write` MUST include `state_field`.
- For `WorkingMemoryLayer`, the agent's `state_visibility.read` MUST include `source_field`.

Failures → `MemoryConfigError` at compile.

## State + checkpointing

Every node boundary in the LangGraph runtime checkpoints the full state to the configured checkpointer (`10-core-framework.md` § Checkpointer; backends in `Tier 7`). Checkpoint = the Pydantic-validated state object dumped to JSON-compatible form.

Implications:
- All state field types must be JSON-serialisable via Pydantic. Custom Python objects without Pydantic models break checkpointing.
- Large state fields (e.g. raw document content) inflate checkpoint size; agents that produce large intermediate artefacts should write IDs/refs to state and store the bulk content in the artifact store.

Restore semantics:
- A killed run resumes from the last checkpointed state at the next-pending node.
- The state is exactly what was checkpointed; reducers don't re-run on restore.

Backwards compatibility on schema migration:
- Adding a field with a default → existing checkpoints load fine (default fills in).
- Removing or renaming a field → existing checkpoints fail to load; checkpoint schema migration is a governance question (truncate vs migrate vs replay).

The recommended pattern for breaking schema changes: bump `StateSpec.schema_version`, write a `v1_to_v2` migration in `state_compiler.py` that maps old checkpoints forward.

## State + observability

Every state mutation emits a `state.transition` event (`10-core-framework.md`). Attributes:

- `agent`: who wrote.
- `fields_written`: list of field names.
- `bytes_delta`: size change of the relevant fields.

Reducer behaviour is observable: an `APPEND` field's `bytes_delta` reflects only the appended chunk, not the full field size. Useful for spotting unbounded-append leaks (`messages` growing forever).

Per-field size monitoring is NOT a built-in alert in v1. It's possible from the metric stream:

```
foundry obs state-size --project <name> --field messages --since 7d
```

Returns the distribution of `messages` field size across runs. Easy to set thresholds externally.

## How state compiles to LangGraph

```
StateSpec (YAML)
   │
   ▼
state_compiler.compile(spec)
   │
   ├── parse each field's type → Python type
   ├── construct Pydantic model: ProjectState(BaseModel)
   ├── for each field, attach reducer metadata:
   │       Annotated[<type>, ReducerMeta(<reducer>)]
   ├── construct per-agent input/output TypedDicts from visibility
   └── return CompiledState:
       - full_model: ProjectState
       - reducers: dict[field_name, reducer_callable]
       - agent_views: dict[agent_name, (InputTypedDict, OutputTypedDict)]
   │
   ▼
foundry.runtime.langgraph_adapter
   │
   ├── builds StateGraph(state_schema=ProjectState)
   ├── attaches reducer dict for fields requiring annotation
   ├── builds subgraphs per agent using agent_views
   └── wires nodes per SystemSpec.flow
```

The Pydantic-first approach lets us emit JSON Schema for the state, which feeds into:
- IDE hints for `state.yaml` editing.
- API endpoint generation (`POST /run` request body shape).
- Eval-set generation (the meta-agent knows the input shape).

## Field name conventions

The conventions are non-binding but enforced by lint when promoting to catalog:

- **`snake_case` for field names.** Pydantic + Python idiom.
- **`messages` for conversation history.** Reserved by convention.
- **`*_at` for timestamps** (`detected_at`, `resolved_at`).
- **`*_id` for IDs** (`trade_id`, `run_id`).
- **`*_ref` for ArtifactRefs** (`tool_ref`, `connection_ref`).
- **Singular for single-value fields** (`investigation_result`), plural for collections (`detected_breaks`).
- **Avoid `state_*` prefixes** — everything is in state already.

The catalog promotion lint warns on convention violations; doesn't block.

## Examples

### Minimal single-agent state

```yaml
schema:
  messages:
    type: list[FoundryMessage]
  final_output:
    type: str | None
    default: null

reducers:
  messages: append

visibility:
  hello_agent:
    read: [messages]
    write: [messages, final_output]

schema_version: 1
```

### Three-agent investigation pipeline

```yaml
schema:
  current_exception:
    type: BaseModel:types:Exception
  classification:
    type: BaseModel:types:Classification
    default: null
  evidence:
    type: list[BaseModel:types:Evidence]
  recommendation:
    type: BaseModel:types:Recommendation
    default: null
  messages:
    type: list[FoundryMessage]

reducers:
  messages: append
  evidence: append

visibility:
  classifier:
    read: [current_exception]
    write: [classification]

  investigator:
    read: [current_exception, classification]
    write: [evidence]

  recommender:
    read: [classification, evidence]
    write: [recommendation, messages]

schema_version: 1
```

The classifier doesn't see evidence (it didn't exist when it ran). The investigator doesn't see the recommendation (it doesn't make recommendations). The recommender doesn't see the raw exception (its inputs are the classifier's and investigator's outputs). Each agent has the minimum visibility it needs.

### Conversational agent with memory

```yaml
schema:
  messages:
    type: list[FoundryMessage]
  user_id:
    type: str
  synthesised_preferences:
    type: str
    default: ""

reducers:
  messages: append
  synthesised_preferences: last_write_wins

visibility:
  assistant:
    read: [messages, user_id, synthesised_preferences]
    write: [messages, synthesised_preferences]

schema_version: 1
```

The assistant has working + semantic memory configured; both layers point at fields in this state. `synthesised_preferences` is consolidated from `messages` periodically.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Field type unparseable | `ConfigError` at load with the failing type string |
| Field references unknown user class | `ConfigError` with import resolution path |
| Visibility entry missing for an agent in `SystemSpec.agents` | `StateVisibilityError` at compile |
| Visibility references unknown field | `StateVisibilityError` at compile |
| Memory layer references field outside agent's visibility | `MemoryConfigError` at compile |
| Reducer maps to unknown field | `ConfigValidationError` at load |
| Field type isn't JSON-serialisable | `CheckpointWriteError` on first checkpoint attempt |
| Schema migration needed across versions | `CheckpointReadError` on resume; needs explicit migration |

## Invariants

1. **State is Pydantic.** Every field has a typed Python representation.
2. **State is JSON-serialisable.** Required for checkpointing. Custom types must use Pydantic models.
3. **Visibility is enforced structurally.** Agents see only their `read` fields; they can write only their `write` fields. No runtime reference can violate this because the projection is by construction.
4. **Reducers are deterministic for the same input order.** Given the same writes in the same order, the merged state is identical.
5. **Append-style fields can grow without bound.** The framework doesn't auto-trim; agents using `APPEND` must consider runaway-growth scenarios (memory layers + state-size observability are the mitigation).
6. **The default reducer is `LAST_WRITE_WINS`.** Explicit annotation is encouraged but not required.
7. **State is not shared across runs.** Each run starts fresh from the project's input. Cross-run state requires either checkpointing the same `run_id` (resume) or persistent memory (`v1.1`).
8. **Compile fails on visibility holes.** Every agent in `SystemSpec.agents` has a visibility entry; entries reference real fields.

## Test expectations

### Unit

1. **Type parsing**: every supported primitive, container, optional, user Pydantic, and `FoundryMessage` parses to the expected Python type.
2. **Reducer attachment**: a state with mixed reducers compiles to a `ProjectState` Pydantic model whose `Annotated` fields carry correct reducer metadata.
3. **Visibility validator**: `read: [unknown]` → `StateVisibilityError` at compile.
4. **Visibility coverage**: a SystemSpec with 3 agents but visibility entries for only 2 → `StateVisibilityError`.
5. **Per-agent TypedDict generation**: an agent with `read: [a, b], write: [c]` → input type contains `a`, `b`; output type contains `c`; full state contains everything.
6. **Reducer behaviour — APPEND**: two writes `[1, 2]` and `[3]` → final `[1, 2, 3]`.
7. **Reducer behaviour — MERGE**: two writes `{a: 1}` and `{a: 2, b: 3}` → final `{a: 2, b: 3}`.
8. **Reducer behaviour — REPLACE_IF_SET**: write `None` after a real value → real value preserved; write a new real value → replaced.
9. **JSON-serialisability check**: a state with a non-serialisable field raises at the appropriate compile point.
10. **Checkpoint round-trip**: dump state to JSON, reload, assert equality.

### Contract

1. **No runtime reference to forbidden fields**: a test agent that tries to access `state.<field-not-in-read>` throws `AttributeError` (the field literally isn't in the projection).
2. **Memory layer state-field check**: memory config referencing a state field outside the agent's `read` visibility → `MemoryConfigError` at compile.
3. **State JSON Schema generation**: emitting JSON Schema for `ProjectState` produces a valid Draft-2020-12 schema.

### Integration (Phase 2 exit gate)

1. **Three-agent pipeline with scoped state**: classifier writes classification; investigator reads classification + writes evidence; recommender reads both. Each agent's view contains only its `read` fields. Test confirms via assertion in the agent body.
2. **State persists across run interruption**: kill the run after classifier completes; restart; investigator picks up with classification populated, evidence empty, runs as expected.
3. **`StateVisibilityError` on visibility hole**: removing visibility entry for `recommender` → compile fails before the run starts.

## Open questions

1. **Field-level redaction**. State fields may contain sensitive data (PII, credentials accidentally returned by a tool). Should `StateSpec` support a `redact_in_observability: bool` per field? Lean: yes — additive flag; defaults to false. Useful for capture_inputs/outputs filtering. Add as a small extension.
2. **Field-level access audit**. Beyond compile-time visibility, should there be runtime audit events when an agent reads/writes specific fields? Lean: no in v1 — too noisy; aggregate `state.transition` events already cover write-side audit. Reads are implicit in the projection.
3. **State diff in audit events**. Currently `state.transition` carries `fields_written` + `bytes_delta`. Should it also carry the actual diff (old → new value)? Lean: opt-in via `ObservabilityConfig.capture_state_diff: bool = false`. Useful for debugging; noisy for production.
4. **Cross-agent shared scratchpad**. Some patterns benefit from a shared mutable dict that any agent can read/write. Currently this is `scratchpad: dict[str, Any]` with `merge` reducer. Should there be a more structured "shared workspace" primitive? Lean: no — `dict[str, Any]` covers it; structured workspace is overkill for v1.
5. **State versioning across deploys**. When `state.yaml` schema_version bumps, in-flight runs continue with their original schema. New runs use new schema. Migration of long-running checkpoints is the operator's call. Worth documenting more explicitly in `50-versioning-model.md`.
