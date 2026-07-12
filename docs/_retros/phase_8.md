# Phase 8 retro

**What took longer than expected.** Testing streaming disconnects. The
plan assumed the ASGI test transports could exercise "kill the client
mid-SSE"; both starlette's TestClient and httpx's ASGITransport turn out
to BUFFER streaming responses to completion before yielding a byte
(measured with a timing probe, not assumed), so the first version of the
kill-mid-stream test deadlocked against a gated mock provider. The fix
reframed what the in-process test can honestly claim: drive the
disconnect at the exact seam starlette closes on a real drop — the
response-body generator's `aclose()` — and push the socket-level variant
(curl + Ctrl-C against live uvicorn) to the manual checklist. The load
test caught a second latent assumption the same way: spawning runs
directly from a test thread has no event loop, which is precisely why
RunManager only ever spawns from inside the lifespan task group.

**What went better than planned.** The run manager composed out of
existing primitives with almost no runtime changes. `run_project` already
did the hard things (checkpointed resume, approval_response
continuation, sequence-continuing emitters, per-run pool close), so the
API layer's whole job became "run it inside a lifespan nursery, fan its
sink out to queues, and persist through the same artifact writer the CLI
uses" — which is why `foundry resume` and `approvals list` see API runs
with zero glue. The sustained-load number was a pleasant surprise: ~150
runs/sec on ONE worker with p95 ~150ms against the mock provider, with
contiguous artifact sequences and balanced pool counters throughout.

**What changed from the plan.** Three deferrals worth naming: native
provider streaming (llm.delta is one synthesized delta per text block —
the wire contract is right, the granularity isn't yet), the Postgres
checkpointer (multi-worker serving is therefore single-host/sqlite; the
docs/85 multi-host shape is documented, not shipped), and mid-graph
`inject_input` (v1 semantics: it starts the socket's next run, which is
what makes the exit gate honest end-to-end). None blocks Phase 9; all
three are on the v1.1+ list with their upgrade paths written down in the
handoff.

**What Phase 9 needs to watch.** (1) `LiveRun.sink` sees every event
exactly once — hang the SQLite observability mirror there, not on a
second reader of events.jsonl. (2) The API layer emits no spans yet;
when the `foundry.api.*` middleware spans land, keep them OUT of the
per-event hot path (the sink is called synchronously inside the run).
(3) The starlette TestClient deprecation (httpx → httpx2) is filtered in
pyproject; whoever bumps starlette owns the swap. (4) `docker build`
should reuse `foundry serve`'s env-factory path — it already carries
everything through the environment.
