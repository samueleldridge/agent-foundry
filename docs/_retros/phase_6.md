# Phase 6 retro

**What took longer than expected.** Testing the loop without a live
model. The design that made it tractable: split the fake LLM into TWO
personalities behind one `httpx.MockTransport` — the meta-agent side is a
SCRIPTED list of tool_use turns (so the tests control exactly what the
"configurator" tries to do), while the forged project's side is a
COMPUTED responder whose correctness is gated on marker strings in the
live system prompt. That second half is what makes the exit gate honest:
the eval score only moves because the meta-agent really wrote a prompt
file, really moved a pin, and really committed — the transport never
fakes a score. Once that harness existed, the hero test (bootstrap →
2 improvement iterations → threshold) passed on the first run.

**The bug the loop found.** `catalog.loader` caches artifact modules by
file path (correct for immutable catalog versions), which means the
meta-agent's core workflow — rewrite `handler.py`, re-run the standalone
eval — would silently re-run the STALE handler. Nothing in Phases 2–5
could hit this because nothing legitimately rewrote a version's files.
`invalidate_artifact_module` + a `write_file` hook fixed it; the hero
test's fail→fix→pass tool-eval sequence now proves re-import happens.

**What changed from the plan.** (1) The meta-agent runs each iteration as
one bounded `run_project` invocation over a synthetic single-agent
CompiledProject rather than one long conversation — directives carry
score/clusters/history/notes forward, which keeps every iteration
checkpointed, replayable, and cheap to script in tests. (2) Sandbox
violations terminate the whole forge (`sandbox_violation`) instead of
docs/60's "note and continue" — the task exit gate's "raises and aborts"
reading won for a security boundary. (3) Interactive mode, forge resume,
and the propose-before-apply `IterationProposal` hop were consciously cut
(they're one feature: human checkpoints); autonomous mode is the v1
deliverable and the exit gate never touches them.

**Belt AND braces held.** The invariant that paid off: prompt rules are
guidance, tool-layer checks are the guarantee. The violation path is
structural three times over — the write refuses, the cancel token fires,
and the session cross-checks `ForgeRecords.violations` even if the
exception got swallowed en route. The forbidden-git-verb guard exists on
top of the fact that no forbidden verb is even exposed as a tool.

**Framework friction worth recording.** The Phase 5 review's pre-work
(explicit rollback file-sets, `--end-of-options`, promote branch gate)
was exactly the right thing to do FIRST — the rollback meta-tool and the
regression demo sit directly on that surface. One residual: temp-repo
fixtures must mirror the real `.gitignore` (`projects/*/.foundry/`), or
audit appends dirty the tree and block clean-tree pre-flights; the
fixture now writes it, and it's called out in the manual tests.

**Cost of not switching frameworks.** None felt. The "meta-agent is just
an agent" bet paid: zero new runtime code, the LLM ⇄ tool loop, budget
enforcement, checkpointing, and events all came from Phases 1–3 for free.
