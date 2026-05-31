# Phase handoff notes

Per-phase notes written by each phase's **implementation** Claude Code session at session end, before it stops. Read by the next phase's implementation session and by the review session.

A handoff note records what the session actually built (vs. what the spec asked for): pinned dependency versions chosen, deviations from spec with rationale, files created, confirmation of each exit-gate check, and any context the next sub-phase needs (e.g., "2a shipped this connection interface — 2b will consume it like this").

Filename convention: `phase_<N>.md` (or `phase_2a.md`, `phase_2b.md`, `phase_2c.md` for split sub-phases). Written by the implementation prompts defined in [`../90-implementation-plan.md`](../90-implementation-plan.md).
