# Phase 0 demo — skeleton smoke

Written retroactively at v1 close-out (2026-07-13); Phase 0 predated the retro/demo convention. The hero commands and their verified outputs at the Phase 0 gate:

```bash
$ uv sync                      # clean-clone install from uv.lock — succeeded (verified twice from deleted .venv + lock)
$ python -m foundry --help     # exit 0; help lists the planned subcommands as phase-gated stubs
$ uv run ruff check src/       # zero violations
$ uv run pytest tests/         # 20 passed (import smoke across all 19 subpackages)
```

Boundary-lint spot check (manual at the time; later pinned by `tests/contract/test_import_boundaries.py`): `import anthropic` inside `src/foundry/core/` fired TID251; the same import inside `src/foundry/providers/anthropic.py` was correctly allowlisted.
