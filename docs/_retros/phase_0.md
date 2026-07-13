# Phase 0 retro — decisions & skeleton

Written retroactively at v1 close-out (2026-07-13); Phase 0 predated the retro/demo convention.

Phase 0 landed the repo skeleton in one session: pinned deps under `uv`, 19 importable subpackages, the two-file ruff import-boundary configuration, pytest config, and the `python -m foundry --help` stub CLI. The one genuinely tricky discovery was that ruff's `banned-api` tables REPLACE rather than merge across nested configs, which forced the root + `src/foundry/core/ruff.toml` split and re-declaration of the third-party bans — documented in the handoff and inherited unchanged by every later phase. The 18-vs-19 subpackage off-by-one in the exit-gate text was resolved in favor of the deliverables list. What Phase 1 needed to watch (and did): keeping the boundary lint honest with contract tests instead of manual verification.
