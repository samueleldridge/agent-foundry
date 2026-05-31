# Manual smoke tests

Per-phase hands-on checklists the **operator** runs after the Claude Code review session has reported PASS, before moving to the next phase.

## Why these exist

The Phase N review session reads code, runs the test suite, and reports PASS / PARTIAL / FAIL. That's necessary but not sufficient — review sessions only see what they can read or invoke. The manual smoke test catches what review sessions miss:

- **Observable behavior in your terminal** — does the output look right to a human?
- **Real LLM + real provider credentials** — does the agent actually produce a greeting from Anthropic when run with your `ANTHROPIC_API_KEY`?
- **Adversarial probes** — when you intentionally break the YAML, is the error message useful or cryptic?
- **Boundary enforcement under attack** — when you intentionally violate an import rule, does lint actually fail?
- **Operator ergonomics** — does the help text read well? Are command names memorable?

A green test suite + a green review session + a green manual smoke test is the three-legged stool that lets you move to the next phase with confidence.

## How to use

1. The Claude Code review session for Phase N has just reported PASS.
2. Open `phase_<N>.md` in this directory.
3. Run each test in order, top to bottom. Each one tells you the command, the expected output, and what to do if it fails.
4. Tick the checkbox at the bottom of each test when it passes.
5. When every box is ticked, sign off at the end of the document.
6. Only then begin Phase N+1.

If a test fails, the test's "If it fails" guidance tells you whether to open a fresh implementation session with a targeted fix prompt or whether it's a config issue you can resolve in place.

## What these are NOT

- **Not a re-run of `pytest`** — `uv run pytest tests/` is automated; that's a precondition, not a manual test.
- **Not a re-read of the design docs** — that's what the Claude Code review session does.
- **Not a substitute for the exit gate** — every item in `docs/03-development-phases.md § Phase <N>` exit gate must pass via the review session OR a manual test here. The two together cover the gate.

## Convention

Each `phase_<N>.md` has the structure:

```
# Phase N — Manual Smoke Tests
## Preconditions      ← what must be true before you start
## Setup              ← env vars, files to stage
## Tests              ← numbered tests with command + expected + failure-mode
## Sign-off           ← checkbox to record completion
```
