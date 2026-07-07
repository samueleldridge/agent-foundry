# Phase 1 handoff — core framework + providers + config + `foundry run`

**Session date:** 2026-07-07
**Branch:** `main`
**Status:** Phase 1 implementation complete; awaiting AI review + operator
manual smoke test (live API keys required — none exist in the dev sandbox,
so every live-path assertion below was verified against
`httpx.MockTransport` fakes with only the HTTP layer substituted).

## What this session built

1. **`foundry.core`** (completing a prior session's in-progress work) —
   Node/Agent/FunctionNode protocols + BaseAgent/BaseFunctionNode;
   LifecycleHooks; Tool/Connection/Embedder/Retriever/Reranker/Memory/Cache
   protocol stubs (implementations land 2a–2c); Session + RunId (monotonic
   ULID) + CancelToken + CheckpointerHandle + CostBudget; FoundryMessage +
   ContentBlock discriminated union; ModelResponse/ModelDelta/StopReason/
   TokenUsage (incl. `reasoning_tokens`); full RunEvent + InboundMessage
   tagged unions; StateBase + Reducer; full FoundryError hierarchy with
   `to_dict()`.
2. **`foundry.providers`** — Provider protocol; httpx-based ProviderAdapter
   base (budget pre-check/post-record, retries + backoff, per-attempt
   timeout, error classification, run_id logging); Anthropic (Messages API)
   + OpenAI (Chat Completions) adapters; registry with the exact exit-gate
   error (`unknown provider 'foo'; available: anthropic, openai`);
   capability-required compile check with supported-by hints; JSON model
   manifests + pricing; ModelBinding/ModelSettings/ProviderCapabilities.
   Bedrock/Azure/Vertex remain unregistered docstring stubs.
3. **`foundry.config`** — SafeLoader-only YAML pipeline (parse → extends →
   env interpolation → secret scan → Pydantic) with structured errors
   (file + JSON pointer + line/column + received-vs-expected + difflib
   did-you-mean hints); schemas per docs/12 (SystemSpec, AgentSpec,
   StateSpec, ToolSpec, ConnectionSpec + ConnectionBinding, EvalSpec,
   FunctionNodeSpec, FlowSpec union); `extends` one-deep + `${ENV:NAME:default}`;
   secret-literal scan (+ `# foundry:allow-literal` pragma) and
   EnvSecretsProvider.
4. **`foundry.runtime.langgraph_adapter`** — compile_project (single flow,
   output-schema import, provider resolution) + run_project (one-node
   StateGraph, full event stream). Only this file + `_langgraph_types.py`
   import langgraph (lint- and contract-test-enforced).
5. **CLI** — `foundry run <project-path> --input '{...}'` (also
   `python -m foundry run ...`). Exit codes: 0 success / 1 run failure /
   2 config-compile failure.
6. **`projects/hello/`** — the exit-gate example project.
7. **Observability slice** — `~/.foundry/runs/<run_id>/` artifact
   (metadata.json + events.jsonl + llm_calls.jsonl) and structlog logging
   with `run_id` bound on every line. `FOUNDRY_HOME` overrides the root.

## Env vars needed for live runs

| Variable | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | anthropic provider (default `credentials_ref`) |
| `OPENAI_API_KEY` | openai provider (default `credentials_ref`) |
| `FOUNDRY_HOME` | optional; overrides `~/.foundry` artifact root |

Models developed against (must exist in `src/foundry/providers/manifests/`):
anthropic `claude-haiku-4-5` (hello default), `claude-sonnet-4-5`,
`claude-opus-4-7`; openai `gpt-4o-mini`, `gpt-4o`, `gpt-5`, `gpt-5-mini`,
`o3-mini` (reasoning — use for manual Test 6). Unknown model ids fail fast
naming the known models; extend the manifest JSON if your account's model id
differs (the `~/.foundry/model_overrides.json` override file is deferred).

## Example project

`projects/hello/` — `system.yaml`, `state.yaml`,
`agents/hello_agent/{agent.yaml, prompts/v1.md, output_schema.py}`.
Hero command:

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

## Deviations from the docs (all deliberate, all Phase 1-scoped)

1. **Providers call vendor HTTP APIs directly via httpx**, not the
   LangChain `init_chat_model` bridge sketched in docs/11 § Module layout.
   Rationale: Phase 1 needs one non-streaming call; direct httpx is
   smaller, keeps the langchain surface confined to the runtime adapter,
   and is trivially fakeable with `httpx.MockTransport` (which the
   phase-1 exit gate mandates for tests). Revisit at Phase 3 when real
   streaming lands; `_langchain_bridge.py` was NOT created.
2. **`foundry.config` imports `foundry.providers`** (for `ModelBinding` in
   `AgentSpec`), whereas docs/12 says config imports core only. docs/11
   places ModelBinding in providers; something had to give. No cycle:
   providers imports only core. Alternative (move ModelBinding to core)
   can be taken up in review.
3. **`Provider.generate()/stream()` take an optional `session` parameter**
   (not in the docs/10/11 signature) — the cost-budget check and
   cancellation need the Session at call time.
4. **`stream()` is a synthesised single-delta wrapper over `generate()`**
   until Phase 3 (`--stream`) lands.
5. **Fields named `schema` use Pydantic aliases** (`StateSpec.state_schema`,
   `OutputSchemaRef.schema_ref`) because a literal `schema` field shadows
   `BaseModel.schema` and warns at class creation. YAML keys are still
   `schema:` as documented.
6. **Hints use difflib** (`get_close_matches`), not true Levenshtein —
   docs/12 calls the hint "best-effort", and difflib is stdlib.
7. **Phase-scoping omissions** per instructions: AgentSpec has no
   `semantic_cache`/`retrievers`/`memory`; ToolSpec has no
   `cacheable`/`cache_ttl_s`/`cache_scope`; no RateLimiter/CircuitBreaker
   (docs/11 sketches them; not on the Phase 1 deliverable list); no
   `CatalogIndex`/`VersionsMetadata` (Phase 2a); `refs.py`/`jsonschema.py`
   remain stubs.
8. **Bedrock/Azure/Vertex are unregistered stubs** so the exit-gate error
   lists exactly `anthropic, openai`.
9. **Guardrails enforcement**: `max_cost_usd` is fully enforced
   (CostBudget). `max_iterations`/`max_hops`/`max_wall_time_s` are parsed
   but not yet enforced — they become meaningful with tool loops and
   multi-agent flows (2a/3/7).

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| `foundry run projects/hello` produces a Greeting from Anthropic | Full path over `httpx.MockTransport` (integration test `test_hello_runs_end_to_end_against_anthropic_fake`); live-key run is manual Test 1 | ✅ (mock) / ⏳ operator |
| Provider swap anthropic→openai with YAML-only change | `test_provider_swap_is_a_yaml_only_change` (copies hello, edits provider+model lines only) | ✅ (mock) / ⏳ operator |
| Unknown provider → structured error naming available providers | Unit + integration + live CLI check; message is exactly `unknown provider 'foo'; available: anthropic, openai` + file + pointer | ✅ |
| Import-boundary lint; zero langgraph/langchain imports outside runtime files | `ruff check` clean; adversarial insert fires TID251 in core/config/providers; contract tests pin both directions | ✅ |
| Invalid YAML shape → error naming file + field + reason | Unit + integration tests; includes line/column + did-you-mean hint | ✅ |
| CostBudget: over-budget → CostBudgetExceeded pre-call; RunFailed; budget context in audit trail | `test_cost_budget_exceeded_pre_call_terminates_run_failed` asserts zero HTTP calls, terminal `run.failed` event with budget context, `metadata.json` budget block | ✅ |
| TokenUsage.reasoning_tokens populated for reasoning models, zero otherwise | Unit tests on OpenAI adapter parsing (o3-mini fixture with `completion_tokens_details`); anthropic always 0 | ✅ |
| Unit tests: provider lookup, capabilities, config loading, env interpolation, secret handling, budget check+record | 157 tests passing (unit + integration + contract) | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (128 files).
- `uv run pytest tests/` — 157 passed.
- `run_id` threaded through logs (structlog bind), events (every RunEvent),
  and artifacts (directory name + every record).
- No secrets in code/configs/fixtures; secret-scan tests build fake values
  at runtime; integration test asserts the API key never appears in output
  or artifacts.

## Context Phase 2a will need

- `ToolRegistry`/`BaseTool`/`RunContext` are structural shells in
  `core/tool.py` — 2a gives them dispatch, validation, and the
  ConnectionAccessor.
- The provider adapters reject `tools=[...]` with a clear error; 2a extends
  `_build_request` for native tool use.
- `compile_project` is deliberately minimal — the real compiler
  (`foundry.orchestration.compiler`) lands Phase 3; don't grow the adapter
  into it.
- `RunArtifactWriter` derives `llm_calls.jsonl` from LLMCallStarted/
  Completed pairs; tool events already have union members waiting.
- dev dep added: `types-pyyaml` (mypy strict).
