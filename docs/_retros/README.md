# Phase retrospectives

One short paragraph per phase, written by the operator after the manual smoke test signs off. What surprised you. What took longer than expected. What changed from the plan. What the next phase needs to watch for.

Cheap to write, valuable in aggregate — patterns emerge across phases that no single retro would surface. Especially useful for catching that a particular kind of error message keeps recurring, or that a compile-time check the spec described as cheap was actually expensive, etc.

Filename convention: `phase_<N>.md` (or `phase_2a.md`, etc. for split sub-phases). Referenced by [`../03-development-phases.md`](../03-development-phases.md) § Phase-gate rituals.
