# Phase 5 retro

**What took longer than expected.** The pin editor. "Update one YAML
value" sounds like `yaml.safe_load` + dump, but the exit gate's real
requirement is a ONE-LINE `git diff` — a round-trip through PyYAML
destroys comments, key order, and quoting across the whole file, which
would turn every rollback commit into an unreviewable reformat. The
indentation-aware block scanner in `versioning.pins` (find the section's
direct children, walk the path, replace only the scalar, keep trailing
comments) is ~90 lines that took several careful passes, and it
deliberately REFUSES anything it can't edit surgically (flow-style
mappings, nested-block "values") rather than guessing. The same
surgical-edit primitive then paid for itself twice: promotion reuses it
to rewrite the copied spec's `version:` line and to insert index.yaml
entries without clobbering comments.

**What changed from the plan.** (1) `GitBackend` shipped sync — the
docs/51 sketch is async, but every Phase 5 consumer is a synchronous CLI
command; wrapping `subprocess.run` in anyio would have added an event
loop to `foundry rollback` for nothing. Phase 6 wraps it in
`anyio.to_thread` when the meta-agent needs it. (2) The "schema
compatibility" pre-flight became a shared `versioning.compat` module
because promotion's semver detection is the SAME comparison from the
other direction (rollback: pinned→target; promote: prior→candidate).
(3) The exit-gate item "schema-incompatible rollback compiles with an
error next run" is proven with a connection-slot regression (v1 requires
a slot system.yaml doesn't bind) — that's the incompatibility class the
Phase 2/3 compiler actually catches structurally; pure input-shape drift
is surfaced by the pre-flight WARNING instead, because no compile-time
consumer validates tool input shapes against agent expectations (agents
bind tools dynamically). Honest statement of the current guarantee, not
a weakened test.

**What was cheaper than expected.** Closing the Phase 4 connection seam.
Because `prepare_connection` + `validate_tool_connection_wiring` +
`SlotConnectionAccessor` were already the compiler's vocabulary,
"standalone tool eval borrows the project's bindings" was ~60 lines in
`load_tool_target` plus a per-case pool in `_tool_invoke` — and it made
connection-requiring tools promotable for free, which was otherwise the
hairiest open question of the phase.

**Friction worth recording.** The audit log vs. clean-tree check almost
deadlocked: docs/52 wants the audit file git-versioned (tamper-evident),
but writing an audit entry AFTER the rollback commit would dirty the tree
and fail the NEXT rollback's pre-flight. Phase 4 had already gitignored
`projects/*/.foundry/`, so the resolution (audit = runtime state, not
committed; tamper-evidence deferred) was inherited rather than invented —
flagged in the handoff in case compliance requirements harden.

**No framework churn.** Zero temptation this phase — subprocess git was
locked by docs/51 and proved exactly as predictable as promised; the one
alternative considered (dulwich for `ls-tree` parsing) wasn't worth a
dependency for three plumbing calls.
