"""foundry.configurator — the meta-agent and its toolkit (docs/60-62).

Dev-time only; never imported by ``foundry.api``. This is the sole module
that composes ``foundry.eval`` + ``foundry.versioning`` as a unit
(architecture rule; other consumers may use either alone).
"""

from __future__ import annotations

from foundry.configurator.meta_agent import (
    ACTIVE_PROMPT_VERSION,
    DEFAULT_META_MODEL_BINDING,
    BoundMetaAgent,
    ForgeGuardrails,
    MetaAgent,
    MetaAgentReport,
    compute_meta_agent_version,
)
from foundry.configurator.session import (
    ForgeError,
    ForgeResult,
    ForgeSession,
    IterationRecord,
    TerminationReason,
    render_summary,
)
from foundry.configurator.tools import (
    MetaToolContext,
    build_meta_tool_registry,
    meta_tool_names,
)

__all__ = [
    "ACTIVE_PROMPT_VERSION",
    "DEFAULT_META_MODEL_BINDING",
    "BoundMetaAgent",
    "ForgeError",
    "ForgeGuardrails",
    "ForgeResult",
    "ForgeSession",
    "IterationRecord",
    "MetaAgent",
    "MetaAgentReport",
    "MetaToolContext",
    "TerminationReason",
    "build_meta_tool_registry",
    "compute_meta_agent_version",
    "meta_tool_names",
    "render_summary",
]
