# Phase 2a retro

**What took longer than expected.** The module-identity bug in the catalog
loader: loading `schemas.py` once per *role* (input_schema, output_schema,
handler's import) produced three distinct-but-identical classes, so
isinstance-based output validation failed on perfectly correct handler
output. The end-to-end smoke caught it (`success: false,
error_category: ToolOutputValidationError` in tool_calls.jsonl while the
run still "passed" because the fake LLM ignored the tool result) — a good
argument for smoke-testing the artifact trail, not just exit codes. Fix was
one line (cache modules by file path); the lesson — dynamic import
identity is part of the tool contract — is now a regression test.

**What changed from the plan.** (1) The manual checklist's allowlist test
assumed a non-zero exit; docs/20's error semantics (dispatcher refuses,
LLM sees a structured is_error tool_result, run recovers) won, and the
checklist was rewritten. (2) docs/12's ArtifactRef sketch (tool +
agent_template kinds) predates the connection catalog; implemented tool +
connection with one resolver, which is what the exit gate actually
measures. (3) `EvalSpec.scope` needed a `connection` member that docs/12
lacked. (4) Multi-field credentials (basic auth, oauth2 client creds,
sigv4) had no specified resolution shape — settled on
"primary-field string OR JSON object" and documented it in the handoff;
worth folding back into docs/23.

**What was cheaper than expected.** State visibility. Pydantic
`create_model` + functional TypedDicts + a dict projection covered
docs/22's structural-enforcement contract in ~250 lines including the
type-string parser; the compile-time validators fell out of the same walk.
The Phase 1 loader's error-formatting investment also paid off — every new
compile-time error (slot wiring, config-vs-schema, visibility) reused the
message-shape conventions for free.

**Friction worth flagging.** (a) Flat 5-file tool dirs are not packages,
so docs/20's `from .schemas import ...` can never work; the `schemas`
alias trick is fine but is exactly the kind of invisible convention that
should be in the tool-authoring docs before the meta-agent starts
scaffolding tools (Phase 6). (b) Two overlapping visibility declarations
(agent.yaml `state_visibility` + state.yaml `visibility`) forced a
must-match rule; one of them should probably be derived, not declared —
candidate for a Tier-2 docs amendment. (c) zsh command-substitution
mangled a backticked commit message once; `git commit -F -` with a
heredoc is the safe pattern.

**What Phase 2b should watch.** (1) The cache lookup/store steps slot into
`ToolRegistry.dispatch` between input validation and the retry loop —
keep dispatch the single entry point rather than wrapping it. (2)
`pgvector`'s `embedding_dimensions` lives in the prepared connection's
validated config — the dimension-match check should read it there, not
re-parse YAML. (3) The pool has no background tasks (idle TTL is stored,
unenforced); if 2b's caches want TTL eviction, build the shared
housekeeping loop once. (4) postgres/pgvector factories are load-tested
only — first live asyncpg use will shake out bugs; budget for it.
