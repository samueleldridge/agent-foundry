# Phase 1 retro

**What took longer than expected.** Error-message quality in the config
loader — position-tracked YAML (composing the node graph for line/column),
JSON-pointer conversion, and picking the *most useful* Pydantic error out of
a multi-error ValidationError (extra_forbidden with a did-you-mean beats a
bare "field required") took as long as the loader pipeline itself. Worth it:
the exit-gate error strings now match docs/12's example shape byte-for-shape.

**What changed from the plan.** Two real deviations: providers speak to
vendor HTTP APIs directly over httpx instead of the docs/11 LangChain
bridge (smaller, and the no-API-keys constraint pushed toward
`httpx.MockTransport`-testable adapters), and `foundry.config` imports
`ModelBinding` from `foundry.providers` (docs/11 and docs/12 disagree about
where ModelBinding lives; review should pick a side — moving it to `core`
is the clean fix). Also discovered: a prior in-progress session had already
built ~80% of `foundry.core` to spec; it needed only lint/mypy fixes, a
monotonic `RunId.new()`, and removal of two `_ = Any` hacks.

**What Phase 2a should watch.** (1) The provider adapters hard-reject
`tools=[...]` — tool dispatch means extending `_build_request` per provider
AND deciding how ToolUseBlock round-trips through the httpx path; budget the
time. (2) `compile_project` in the runtime adapter is a placeholder for the
real Phase 3 compiler — resist growing it. (3) pytest `filterwarnings=error`
plus structlog/capsys interact badly with cached loggers (fixed by resolving
stderr at logger-creation time) — keep that in mind for any new logging.
(4) The live-key manual smoke test has not run yet; treat any drift it finds
as Phase 1 fix-work, not 2a scope.
