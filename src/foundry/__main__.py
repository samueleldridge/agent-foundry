"""`python -m foundry` shim — delegates to `foundry.cli.__main__`."""

from __future__ import annotations

from foundry.cli.__main__ import main

if __name__ == "__main__":
    main()
