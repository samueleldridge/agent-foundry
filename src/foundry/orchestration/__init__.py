"""Orchestration: state compilation + visibility (2a); the five-pattern
flow compiler, predicate sandbox, handoff-tool generation, and HITL
pause/resume semantics (Phase 7).

``foundry.orchestration.compiler`` is imported directly by consumers (not
re-exported here): it depends on ``foundry.runtime.compiled`` for the
CompiledSystem value type, and keeping the package import light avoids
coupling every state_scope consumer to the full compile pipeline.
"""

from __future__ import annotations

from foundry.orchestration.hitl import (
    ApprovalResolution,
    InterruptPayload,
)
from foundry.orchestration.patterns import (
    END_SENTINEL,
    SUPPORTED_PATTERNS,
    FlowPlan,
    plan_flow,
)
from foundry.orchestration.predicates import (
    CompiledPredicate,
    compile_predicate,
)
from foundry.orchestration.state_scope import (
    AgentStateView,
    CompiledState,
    compile_state,
    parse_type_string,
)

__all__ = [
    "END_SENTINEL",
    "SUPPORTED_PATTERNS",
    "AgentStateView",
    "ApprovalResolution",
    "CompiledPredicate",
    "CompiledState",
    "FlowPlan",
    "InterruptPayload",
    "compile_predicate",
    "compile_state",
    "parse_type_string",
    "plan_flow",
]
