# Phase 9 retro (final phase)

**What took longer than expected.** Nothing structural — the phase's real
tax was breadth, not depth: ten loosely-coupled deliverables (three
observability transports, storage, security, testing, four CLIs, TUI,
deploy, packaging) each small, but every one needing the full
ruff/mypy-strict/full-suite gate. Two genuine surprises cost debugging
time. First, the shared credential denylist regex matches `token` — which
silently swallowed every token-COUNT field (`input_tokens`,
`saved_tokens_estimate`) from span attributes on the first observability
run; the fix exempts the plural-`tokens` count shape and is documented in
`redaction.py`, but it's a good reminder that default-deny redaction
needs positive tests for what must SURVIVE redaction, not just for what
must be dropped. Second, wiring the docs/83 tool-result boundary broke
the Phase 6 forge tests in a subtle way: the deterministic fake LLM
parses tool_result JSON to decide its next scripted move, and the typed
boundary is — by design — part of what the model now sees. The fix
(`unwrap_tool_output` for fakes) is honest: a real model sees the
boundary too.

**What went better than planned.** The event stream earned its keep. All
three observability transports hang off ONE hook in `EventEmitter.emit`,
which means CLI runs, API runs, and eval per-case runs got spans, metrics,
and the SQLite mirror simultaneously, with no per-surface wiring — the
audit-trail-first design from Tier 0 paid out exactly as promised.
Span-mirroring events retroactively (start time back-computed from
`latency_ms`) avoided touching `foundry.core` at all, keeping the
import-boundary invariant intact. The LangSmith/Langfuse "exporters"
turned out to need zero vendor SDKs — both ingest OTLP directly, so the
adapters are ~40 lines of endpoint + auth-header configuration each. And
the marker-gated mock pattern from the Phase 6 forge tests made the
5-minute demo genuinely honest: the regression the demo rolls back is a
real behavioural regression caught by the real eval harness, in 0.7s.

**What changed from the plan.** textual was dropped in favour of rich for
the review TUI (a full TUI framework pin for one read-mostly page wasn't
warranted; the ReviewModel/render split leaves a textual upgrade path).
The SQLite mirror ships the five deliverable tables — forge/rollback
mirror tables from the docs/80 sketch wait for a v1.1 `foundry obs forge`
query surface, since the audit log already holds the regulatory record.
Docker build/run, live OTel collector export, multi-host S3, and the
live-key forge demo all moved to the manual checklist — the sandbox had
no daemon, no collector, and no keys, and pretending otherwise would have
meant testing a mock of a mock.

**What v1.1 should watch.** (1) The obs CLI covers cost/failures/latency/
runs/eval-trend; `foundry obs trace <run_id>` tree-rendering and
audit-via-obs are the next most-asked-for surfaces and the store already
holds what they need. (2) The span mirror parents subsystem spans to
whatever is current at emit time — correct today because emits happen
inside node/llm scopes, but if a future runtime emits events from
detached tasks the parenting should be revisited. (3) Retention is
implemented for the runs tree; when eval/forge artifacts move to their
own docs/81 directories, retention needs per-kind scan roots. (4) The
per-artifact secret scan patterns and the observability denylist share
one regex module — extend them together via `~/.foundry/
secret_patterns.yaml` (already documented) rather than forking.

**v1 ships here.** 983 tests, 10 phases, zero open exit-gate items on the
automated side. The remaining trust is operational: the manual sheet
(docker, collector, live forge) and real projects.
