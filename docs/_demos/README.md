# Phase demo records

Per-phase smoke-test demo: the hero command for the phase, run from a clean checkout in a fresh venv, with its output captured and dated. The artifact a future operator (or you in three months) consults to confirm "this is what working looks like."

A demo record is the receipt the phase actually ran end-to-end, not just that its tests passed. If a regression later breaks the hero command, the demo file is the diff target.

Filename convention: `phase_<N>.md` (or `phase_2a.md`, etc.). Referenced by [`../03-development-phases.md`](../03-development-phases.md) § Phase-gate rituals.
