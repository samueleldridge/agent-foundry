"""Foundry Studio — the dev-time control plane behind the Studio webapp
(docs/72).

Like ``foundry.cli`` and ``foundry.configurator``, this module composes
configurator + eval + versioning + api internals. The run-time serving
layer (``foundry.api``) MUST NOT import it (docs/01 rule 5; enforced by
``src/foundry/api/ruff.toml`` + the import-boundary contract test).
"""

from foundry.studio.app import create_studio_app
from foundry.studio.context import StudioContext, StudioSettings

__all__ = ["StudioContext", "StudioSettings", "create_studio_app"]
