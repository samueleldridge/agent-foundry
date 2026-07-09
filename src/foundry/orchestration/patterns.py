"""Flow-pattern planning: which nodes run, in what order (docs/30).

Phase 3 executes the ``single`` pattern plus the Phase 2c ``sequential``
shape (any number of function nodes + exactly ONE agent). The remaining
patterns — ``parallel``, ``supervisor``, ``graph``, and multi-agent
``sequential`` — are validated for reference resolution here but their
execution compiles in Phase 7; planning them raises a structured
``CompileError`` saying so.

No langgraph imports: patterns produce an :class:`ExecutionPlan` the runtime
adapter turns into StateGraph nodes/edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from foundry.config import LoadedProject, SequentialFlow, SingleFlow
from foundry.core.errors import CompileError

SUPPORTED_PATTERNS = ("single", "sequential")
"""Patterns Phase 3 executes. parallel/supervisor/graph compile in Phase 7."""

RESERVED_SUBNODE_SUFFIXES = ("llm", "tools", "finish", "turn", "turn_end")
"""The runtime expands every agent into ``<agent>__<suffix>`` sub-graph
nodes (docs/_phase_handoffs/phase_3.md deviation 7); those names are
reserved in the flow-node namespace and checked at compile time."""


@dataclass(frozen=True)
class ExecutionPlan:
    """The runnable shape of a validated flow: the (single) agent plus the
    ordered node steps. ``steps`` is empty for a ``single`` flow — the
    adapter builds the agent's sub-graph directly."""

    pattern: str
    agent_name: str
    steps: tuple[str, ...] = ()


def flow_node_refs(flow: Any) -> list[tuple[str, str]]:
    """Every node name a flow references, as (json_pointer, name) pairs.
    Works across all five flow types so reference resolution is validated
    even for patterns whose EXECUTION lands in Phase 7."""
    refs: list[tuple[str, str]] = []
    if flow.type == "single":
        refs.append(("/flow/agent", flow.agent))
    elif flow.type == "sequential":
        refs.extend(
            (f"/flow/steps/{i}", step) for i, step in enumerate(flow.steps)
        )
    elif flow.type == "parallel":
        refs.extend(
            (f"/flow/parallel_branches/{i}", branch)
            for i, branch in enumerate(flow.parallel_branches)
        )
        if flow.join is not None:
            refs.append(("/flow/join", flow.join))
        refs.extend((f"/flow/then/{i}", step) for i, step in enumerate(flow.then))
    elif flow.type == "supervisor":
        refs.append(("/flow/supervisor", flow.supervisor))
        refs.extend(
            (f"/flow/workers/{i}", worker) for i, worker in enumerate(flow.workers)
        )
    else:  # graph
        refs.append(("/flow/start", flow.start))
        for i, edge in enumerate(flow.edges):
            refs.append((f"/flow/edges/{i}/from", edge.from_))
            refs.append((f"/flow/edges/{i}/to", edge.to))
    return refs


def validate_flow_refs(project: LoadedProject, system_file: Path) -> None:
    """Mixed-flow reference resolution (docs/03 § Phase 2c): every from/to/
    step name must resolve to an agent OR a function, interchangeably."""
    known = set(project.system.agents) | set(project.system.functions)
    for pointer, name in flow_node_refs(project.system.flow):
        if name == "END":  # graph flows may target the terminal sentinel
            continue
        if name not in known:
            raise CompileError(
                f"flow references unknown node {name!r} at {pointer}; it is "
                f"neither an agent ({', '.join(sorted(project.system.agents)) or '(none)'}) "
                f"nor a function ({', '.join(sorted(project.system.functions)) or '(none)'})",
                context={
                    "file": str(system_file),
                    "pointer": pointer,
                    "received": name,
                    "agents": sorted(project.system.agents),
                    "functions": sorted(project.system.functions),
                },
            )


def validate_namespace(project: LoadedProject, system_file: Path) -> None:
    """Agents and functions share one node namespace (docs/21): the compiler
    resolves flow steps to either, so names cannot collide. The runtime's
    per-agent sub-node names (``<agent>__llm`` etc.) live in that namespace
    too — a colliding node name is a compile-time error, not a runtime one
    (Phase 3 review finding 4)."""
    collisions = sorted(set(project.system.agents) & set(project.system.functions))
    if collisions:
        raise CompileError(
            f"node namespace collision: {', '.join(collisions)} declared as "
            "BOTH an agent and a function in system.yaml; agents and "
            "functions share one flow-node namespace",
            context={
                "file": str(system_file),
                "pointer": "/functions",
                "collisions": collisions,
            },
        )
    node_names = set(project.system.agents) | set(project.system.functions)
    for agent_name in project.system.agents:
        reserved = {
            f"{agent_name}__{suffix}" for suffix in RESERVED_SUBNODE_SUFFIXES
        }
        taken = sorted(reserved & node_names)
        if taken:
            raise CompileError(
                f"node name(s) collide with agent {agent_name!r}'s reserved "
                f"internal sub-node names: {', '.join(taken)}; rename the "
                f"node(s) (reserved per agent: <agent>__"
                f"{'/'.join(RESERVED_SUBNODE_SUFFIXES)})",
                context={
                    "file": str(system_file),
                    "pointer": "/functions",
                    "collisions": taken,
                    "agent": agent_name,
                },
            )


def plan_flow(project: LoadedProject, system_file: Path) -> ExecutionPlan:
    """Resolve the flow into an executable plan.

    Phase 3 supports ``single`` (one agent) and ``sequential`` (exactly one
    agent + any number of function nodes). The other patterns are stubbed:
    their references are already validated, but planning raises a
    ``CompileError`` pointing at Phase 7.
    """
    flow = project.system.flow
    if isinstance(flow, SingleFlow):
        if flow.agent not in project.agents:
            raise CompileError(
                f"flow.agent {flow.agent!r} is not in SystemSpec.agents "
                f"{sorted(project.agents)}",
                context={"file": str(system_file),
                         "pointer": "/flow/agent", "received": flow.agent},
            )
        return ExecutionPlan(pattern="single", agent_name=flow.agent)
    if not isinstance(flow, SequentialFlow):
        raise CompileError(
            f"the {flow.type!r} flow pattern compiles in Phase 7 "
            f"(multi-agent orchestration); Phase 3 executes only "
            f"{' / '.join(SUPPORTED_PATTERNS)} — its references validated, "
            "but execution is not yet supported",
            context={"file": str(system_file),
                     "pointer": "/flow/type", "received": flow.type},
        )
    agent_steps = [step for step in flow.steps if step in project.agents]
    if len(agent_steps) != 1:
        raise CompileError(
            f"sequential flows with {len(agent_steps)} agent steps "
            f"({', '.join(agent_steps) or '(none)'}) compile in Phase 7 "
            "(multi-agent orchestration); Phase 3 sequential flows need "
            "exactly ONE agent step (plus any number of function nodes)",
            context={
                "file": str(system_file),
                "pointer": "/flow/steps",
                "agent_steps": agent_steps,
            },
        )
    return ExecutionPlan(
        pattern="sequential",
        agent_name=agent_steps[0],
        steps=tuple(flow.steps),
    )


__all__ = [
    "SUPPORTED_PATTERNS",
    "ExecutionPlan",
    "flow_node_refs",
    "plan_flow",
    "validate_flow_refs",
    "validate_namespace",
]
