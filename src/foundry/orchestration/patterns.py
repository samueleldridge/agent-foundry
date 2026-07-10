"""Flow-pattern compilation: FlowSpec → FlowPlan (docs/30).

Phase 7 compiles all five patterns — ``single``, ``sequential``,
``parallel``, ``supervisor``, ``graph`` — plus inline NESTING (a
sequential step / parallel branch / supervisor worker may be a
``{<name>: <flow>}`` sub-flow; docs/30 § Composition). The product is a
:class:`FlowPlan` tree of plain dataclasses; the runtime adapter walks it
into StateGraph nodes/edges. No langgraph imports on this side of the
boundary.

The pattern set is CLOSED (docs/30 invariant 1): an unknown ``flow.type``
never reaches this module (the FlowSpec discriminated union rejects it at
load), and adding a pattern means code here + tests + lint, not YAML.

v1 scope notes (documented in the Phase 7 handoff):
- ``handoff_policy.mode`` must be ``llm``; ``rule`` / ``hybrid`` raise a
  structured deferral CompileError.
- ``force_return_to_supervisor`` must stay true; workers hand back to the
  supervisor (or terminate via END when allowed + the termination
  predicate fires). Direct worker→worker transitions are rule-mode
  territory and deferred with it.
- Graph nodes are plain agent/function references (no nested flows as
  graph nodes); a nested ``graph`` inside another pattern is refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union

from foundry.config import (
    GraphFlow,
    LoadedProject,
    ParallelFlow,
    SequentialFlow,
    SingleFlow,
    SupervisorFlow,
)
from foundry.core.errors import CompileError
from foundry.orchestration.predicates import (
    CompiledPredicate,
    compile_predicate,
)

END_SENTINEL = "END"
"""Terminal node sentinel in flow configs. END is a sink (docs/30
invariant 8): nothing may transition FROM it."""

SUPPORTED_PATTERNS = ("single", "sequential", "parallel", "supervisor", "graph")

RESERVED_SUBNODE_SUFFIXES = (
    "llm",
    "tools",
    "finish",
    "turn",
    "turn_end",
    "enter",
    "exit",
    "dispatch",
    "handoff",
    "route",
)
"""The runtime expands agents and sub-flows into ``<name>__<suffix>``
nodes; those names are reserved in the flow-node namespace and checked at
compile time."""


# --- plan tree -----------------------------------------------------------------


@dataclass(frozen=True)
class LeafNode:
    """One agent or function node reference."""

    kind: Literal["agent", "function"]
    name: str


@dataclass(frozen=True)
class SequentialPlan:
    name: str
    steps: tuple[PlanNode, ...]


@dataclass(frozen=True)
class ParallelPlan:
    name: str
    branches: tuple[PlanNode, ...]
    join: LeafNode | None
    then: tuple[PlanNode, ...]


@dataclass(frozen=True)
class SupervisorPlan:
    name: str
    supervisor: str
    """The supervisor AGENT name."""
    workers: tuple[tuple[str, PlanNode], ...]
    """(target_name, plan) pairs — target_name is what handoff tools and
    routing use (the leaf's node name, or the nested flow's name)."""
    supervisor_targets: tuple[str, ...]
    """Worker names (± END) the supervisor may hand off to — the handoff
    tool set (docs/30 § Handoff tool generation)."""
    workers_may_end: frozenset[str]
    """Workers whose allowed_handoffs include END."""
    termination_when: CompiledPredicate | None
    max_hops: int
    on_max_hops: Literal["error", "return_partial", "escalate"]
    escalate_to: str | None


@dataclass(frozen=True)
class CompiledEdge:
    to: str
    """Target node name, or END_SENTINEL."""
    predicate: CompiledPredicate | None
    """None = unconditional (the 'else' edge)."""


@dataclass(frozen=True, eq=False)
class GraphPlan:
    name: str
    start: str
    nodes: tuple[LeafNode, ...]
    edges_by_source: dict[str, tuple[CompiledEdge, ...]] = field(
        default_factory=dict
    )


PlanNode = Union[  # noqa: UP007 - recursive union; X | Y cannot forward-ref
    LeafNode, SequentialPlan, ParallelPlan, SupervisorPlan, GraphPlan
]


@dataclass(frozen=True)
class FlowPlan:
    """The runnable shape of a validated flow."""

    pattern: str
    root: PlanNode
    primary_agent: str
    """The terminal/output agent (docs/31 § Output contract): supervisor
    for supervisor flows, the last agent for sequential, the join/then
    agent for parallel, the last END-edge agent for graph."""
    agents: tuple[str, ...]
    """Every agent the flow uses, in first-appearance order."""
    subflow_names: tuple[str, ...]
    """Inline nested-flow names (they own __enter/__exit graph nodes)."""
    steps: tuple[str, ...] = ()
    """Legacy Phase 2c shape: top-level step names for an un-nested
    sequential flow (empty otherwise). Consumed by the run-result contract
    (a sequential pipeline's product is its final state)."""


# --- reference walking (all patterns, nested included) ---------------------------


def flow_node_refs(flow: Any) -> list[tuple[str, str]]:
    """Every LEAF node name a flow references, as (json_pointer, name)
    pairs, recursing into nested flows."""
    refs: list[tuple[str, str]] = []

    def walk_ref(pointer: str, ref: Any) -> None:
        if isinstance(ref, str):
            refs.append((pointer, ref))
            return
        ((name, nested),) = ref.items()
        walk_flow(f"{pointer}/{name}", nested)

    def walk_flow(pointer: str, node: Any) -> None:
        if node.type == "single":
            refs.append((f"{pointer}/agent", node.agent))
        elif node.type == "sequential":
            for i, step in enumerate(node.steps):
                walk_ref(f"{pointer}/steps/{i}", step)
        elif node.type == "parallel":
            for i, branch in enumerate(node.parallel_branches):
                walk_ref(f"{pointer}/parallel_branches/{i}", branch)
            if node.join is not None:
                refs.append((f"{pointer}/join", node.join))
            for i, step in enumerate(node.then):
                walk_ref(f"{pointer}/then/{i}", step)
        elif node.type == "supervisor":
            refs.append((f"{pointer}/supervisor", node.supervisor))
            for i, worker in enumerate(node.workers):
                walk_ref(f"{pointer}/workers/{i}", worker)
        else:  # graph
            refs.append((f"{pointer}/start", node.start))
            for i, edge in enumerate(node.edges):
                refs.append((f"{pointer}/edges/{i}/from", edge.from_))
                refs.append((f"{pointer}/edges/{i}/to", edge.to))

    walk_flow("/flow", flow)
    return refs


def validate_flow_refs(project: LoadedProject, system_file: Path) -> None:
    """Every leaf from/to/step/worker name must resolve to an agent OR a
    function, interchangeably (docs/30 § Nodes)."""
    known = set(project.system.agents) | set(project.system.functions)
    for pointer, name in flow_node_refs(project.system.flow):
        if name == END_SENTINEL:
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
    """Agents and functions share one node namespace (docs/21); the
    runtime's ``<name>__<suffix>`` expansion names live there too."""
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
    for owner in sorted(node_names):
        reserved = {f"{owner}__{suffix}" for suffix in RESERVED_SUBNODE_SUFFIXES}
        taken = sorted(reserved & node_names)
        if taken:
            raise CompileError(
                f"node name(s) collide with {owner!r}'s reserved internal "
                f"sub-node names: {', '.join(taken)}; rename the node(s) "
                f"(reserved per node: <name>__"
                f"{'/'.join(RESERVED_SUBNODE_SUFFIXES)})",
                context={
                    "file": str(system_file),
                    "pointer": "/functions",
                    "collisions": taken,
                    "owner": owner,
                },
            )


# --- plan compilation -------------------------------------------------------------


class _PlanBuilder:
    def __init__(self, project: LoadedProject, system_file: Path) -> None:
        self.project = project
        self.system_file = system_file
        self.state_fields = set(project.state.state_schema)
        self.max_depth = project.system.guardrails.max_flow_nesting_depth
        self.used_leaves: dict[str, str] = {}
        self.subflow_names: list[str] = []
        self.agents_in_order: list[str] = []

    def error(self, message: str, pointer: str, **context: Any) -> CompileError:
        return CompileError(
            message,
            context={"file": str(self.system_file), "pointer": pointer,
                     **context},
        )

    def leaf(self, name: str, pointer: str) -> LeafNode:
        if name in self.used_leaves:
            raise self.error(
                f"node {name!r} appears more than once in the flow (first "
                f"at {self.used_leaves[name]}, again at {pointer}); each "
                "agent/function occupies exactly one flow position — loops "
                "are expressed via the supervisor or graph patterns, not "
                "by repeating steps",
                pointer,
                node=name,
            )
        self.used_leaves[name] = pointer
        if name in self.project.agents:
            if name not in self.agents_in_order:
                self.agents_in_order.append(name)
            return LeafNode(kind="agent", name=name)
        return LeafNode(kind="function", name=name)

    def subflow_name(self, name: str, pointer: str) -> str:
        known = set(self.project.system.agents) | set(
            self.project.system.functions
        )
        if name in known or name in self.subflow_names:
            raise self.error(
                f"nested flow name {name!r} at {pointer} collides with an "
                "existing agent/function/sub-flow name; sub-flow names live "
                "in the flow-node namespace",
                pointer,
                name=name,
            )
        for suffix in RESERVED_SUBNODE_SUFFIXES:
            if name.endswith(f"__{suffix}"):
                raise self.error(
                    f"nested flow name {name!r} at {pointer} ends with the "
                    f"reserved suffix __{suffix}",
                    pointer,
                    name=name,
                )
        self.subflow_names.append(name)
        return name

    def compile_ref(self, ref: Any, pointer: str, depth: int) -> PlanNode:
        if isinstance(ref, str):
            return self.leaf(ref, pointer)
        ((name, nested),) = ref.items()
        return self.compile_flow(
            nested,
            name=self.subflow_name(name, pointer),
            pointer=f"{pointer}/{name}",
            depth=depth + 1,
        )

    def compile_flow(
        self, flow: Any, *, name: str, pointer: str, depth: int
    ) -> PlanNode:
        if depth > self.max_depth:
            raise self.error(
                f"flow nesting exceeds max_flow_nesting_depth "
                f"({self.max_depth}) at {pointer}; split the system into "
                "multiple projects instead (docs/30 § Composition)",
                pointer,
                depth=depth,
                max_depth=self.max_depth,
            )
        if isinstance(flow, SingleFlow):
            step = self.leaf(flow.agent, f"{pointer}/agent")
            if depth == 0:
                return step
            # Degenerate nesting: a named one-step sequential keeps the
            # sub-flow addressable under its declared name.
            return SequentialPlan(name=name, steps=(step,))
        if isinstance(flow, SequentialFlow):
            steps = tuple(
                self.compile_ref(ref, f"{pointer}/steps/{i}", depth)
                for i, ref in enumerate(flow.steps)
            )
            return SequentialPlan(name=name, steps=steps)
        if isinstance(flow, ParallelFlow):
            return self.compile_parallel(flow, name, pointer, depth)
        if isinstance(flow, SupervisorFlow):
            return self.compile_supervisor(flow, name, pointer, depth)
        if isinstance(flow, GraphFlow):
            if depth > 0:
                raise self.error(
                    f"a nested 'graph' flow at {pointer} is not supported "
                    "in v1; graph is a top-level pattern (Phase 7 handoff "
                    "deviation)",
                    pointer,
                )
            return self.compile_graph(flow, name, pointer)
        raise self.error(  # pragma: no cover - FlowSpec union is closed
            f"unknown flow pattern {getattr(flow, 'type', flow)!r}",
            pointer,
        )

    # --- parallel ---------------------------------------------------------

    def compile_parallel(
        self, flow: ParallelFlow, name: str, pointer: str, depth: int
    ) -> ParallelPlan:
        branches = tuple(
            self.compile_ref(ref, f"{pointer}/parallel_branches/{i}", depth)
            for i, ref in enumerate(flow.parallel_branches)
        )
        join = (
            self.leaf(flow.join, f"{pointer}/join")
            if flow.join is not None
            else None
        )
        then = tuple(
            self.compile_ref(ref, f"{pointer}/then/{i}", depth)
            for i, ref in enumerate(flow.then)
        )
        return ParallelPlan(name=name, branches=branches, join=join, then=then)

    # --- supervisor -------------------------------------------------------

    def compile_supervisor(
        self, flow: SupervisorFlow, name: str, pointer: str, depth: int
    ) -> SupervisorPlan:
        policy = flow.handoff_policy
        if policy.mode != "llm":
            raise self.error(
                f"handoff_policy.mode {policy.mode!r} is deferred; v1 "
                "implements 'llm' (typed handoff tools). Rule-based routing "
                "is the graph pattern's job until rule mode lands "
                "(Phase 7 handoff deviation)",
                f"{pointer}/handoff_policy/mode",
                received=policy.mode,
            )
        if not policy.force_return_to_supervisor:
            raise self.error(
                "force_return_to_supervisor: false is deferred with rule "
                "mode; v1 workers always return to the supervisor (or END "
                "when allowed + the termination predicate fires)",
                f"{pointer}/handoff_policy/force_return_to_supervisor",
            )
        if flow.supervisor not in self.project.agents:
            raise self.error(
                f"flow.supervisor {flow.supervisor!r} must be an AGENT "
                "(LLM-driven routing needs an LLM); function nodes cannot "
                "supervise",
                f"{pointer}/supervisor",
                received=flow.supervisor,
            )
        self.leaf(flow.supervisor, f"{pointer}/supervisor")

        workers: list[tuple[str, PlanNode]] = []
        for i, ref in enumerate(flow.workers):
            plan = self.compile_ref(ref, f"{pointer}/workers/{i}", depth)
            workers.append((plan.name, plan))
        worker_names = [target for target, _ in workers]
        if flow.supervisor in worker_names:
            raise self.error(
                f"supervisor {flow.supervisor!r} cannot also be a worker",
                f"{pointer}/workers",
            )

        # allowed_handoffs: defaults + validation (docs/30 § supervisor).
        allowed = dict(policy.allowed_handoffs)
        unknown_keys = sorted(set(allowed) - {flow.supervisor, *worker_names})
        if unknown_keys:
            raise self.error(
                f"allowed_handoffs references unknown node(s): "
                f"{', '.join(unknown_keys)} (supervisor: {flow.supervisor}; "
                f"workers: {', '.join(worker_names)})",
                f"{pointer}/handoff_policy/allowed_handoffs",
                unknown=unknown_keys,
            )
        supervisor_targets = allowed.get(
            flow.supervisor, [*worker_names, END_SENTINEL]
        )
        bad_targets = sorted(
            set(supervisor_targets) - {*worker_names, END_SENTINEL}
        )
        if bad_targets:
            raise self.error(
                f"allowed_handoffs[{flow.supervisor}] references unknown "
                f"worker(s): {', '.join(bad_targets)} (workers: "
                f"{', '.join(worker_names)}, END)",
                f"{pointer}/handoff_policy/allowed_handoffs/{flow.supervisor}",
                unknown=bad_targets,
            )
        workers_may_end: set[str] = set()
        for worker_name in worker_names:
            targets = allowed.get(worker_name, [flow.supervisor])
            extra = sorted(set(targets) - {flow.supervisor, END_SENTINEL})
            if extra:
                raise self.error(
                    f"allowed_handoffs[{worker_name}] may only contain the "
                    f"supervisor ({flow.supervisor!r}) and END in v1 — "
                    "direct worker→worker transitions are rule-mode "
                    "territory (Phase 7 handoff deviation); got: "
                    f"{', '.join(extra)}",
                    f"{pointer}/handoff_policy/allowed_handoffs/{worker_name}",
                    unknown=extra,
                )
            if END_SENTINEL in targets:
                workers_may_end.add(worker_name)

        termination = flow.termination
        when = (
            compile_predicate(
                termination.when,
                state_fields=self.state_fields,
                where=str(self.system_file),
                pointer=f"{pointer}/termination/when",
            )
            if termination.when is not None
            else None
        )
        if (
            termination.on_max_hops == "escalate"
            and termination.escalate_to not in worker_names
        ):
            raise self.error(
                f"termination.escalate_to {termination.escalate_to!r} is "
                f"not a worker (workers: {', '.join(worker_names)})",
                f"{pointer}/termination/escalate_to",
                received=termination.escalate_to,
            )
        return SupervisorPlan(
            name=name,
            supervisor=flow.supervisor,
            workers=tuple(workers),
            supervisor_targets=tuple(dict.fromkeys(supervisor_targets)),
            workers_may_end=frozenset(workers_may_end),
            termination_when=when,
            max_hops=termination.max_hops,
            on_max_hops=termination.on_max_hops,
            escalate_to=termination.escalate_to,
        )

    # --- graph -----------------------------------------------------------

    def compile_graph(
        self, flow: GraphFlow, name: str, pointer: str
    ) -> GraphPlan:
        node_names: list[str] = []
        for edge in flow.edges:
            if edge.from_ == END_SENTINEL:
                raise self.error(
                    "END is a sink: no edge may originate from END "
                    "(docs/30 invariant 8)",
                    f"{pointer}/edges",
                )
            for endpoint in (edge.from_, edge.to):
                if endpoint != END_SENTINEL and endpoint not in node_names:
                    node_names.append(endpoint)
        if flow.start not in node_names:
            node_names.insert(0, flow.start)

        edges_by_source: dict[str, list[CompiledEdge]] = {}
        for i, edge in enumerate(flow.edges):
            predicate = (
                compile_predicate(
                    edge.when,
                    state_fields=self.state_fields,
                    where=str(self.system_file),
                    pointer=f"{pointer}/edges/{i}/when",
                )
                if edge.when is not None
                else None
            )
            edges_by_source.setdefault(edge.from_, []).append(
                CompiledEdge(to=edge.to, predicate=predicate)
            )

        self._check_graph_shape(flow, node_names, edges_by_source, pointer)
        leaves = tuple(
            self.leaf(node, f"{pointer}/edges") for node in node_names
        )
        return GraphPlan(
            name=name,
            start=flow.start,
            nodes=leaves,
            edges_by_source={
                source: tuple(edges)
                for source, edges in edges_by_source.items()
            },
        )

    def _check_graph_shape(
        self,
        flow: GraphFlow,
        node_names: list[str],
        edges_by_source: dict[str, list[CompiledEdge]],
        pointer: str,
    ) -> None:
        # Reachability from start.
        reachable: set[str] = set()
        stack = [flow.start]
        while stack:
            current = stack.pop()
            if current in reachable or current == END_SENTINEL:
                continue
            reachable.add(current)
            stack.extend(edge.to for edge in edges_by_source.get(current, []))
        unreachable = sorted(set(node_names) - reachable)
        if unreachable:
            raise self.error(
                f"graph node(s) unreachable from start ({flow.start!r}): "
                f"{', '.join(unreachable)}",
                f"{pointer}/edges",
                unreachable=unreachable,
            )
        # Every node has a path to END.
        can_end: set[str] = set()
        changed = True
        while changed:
            changed = False
            for node in node_names:
                if node in can_end:
                    continue
                for edge in edges_by_source.get(node, []):
                    if edge.to == END_SENTINEL or edge.to in can_end:
                        can_end.add(node)
                        changed = True
                        break
        stuck = sorted(set(node_names) - can_end)
        if stuck:
            raise self.error(
                f"graph node(s) with no path to END: {', '.join(stuck)}",
                f"{pointer}/edges",
                stuck=stuck,
            )
        # Cycle detection (DFS colouring) unless cycles_allowed.
        if not flow.cycles_allowed:
            visiting: set[str] = set()
            done: set[str] = set()

            def visit(node: str, trail: list[str]) -> None:
                if node == END_SENTINEL or node in done:
                    return
                if node in visiting:
                    cycle = [*trail[trail.index(node):], node]
                    raise self.error(
                        f"graph contains a cycle: {' → '.join(cycle)}; set "
                        "cycles_allowed: true (and rely on "
                        "guardrails.max_hops) to permit it",
                        f"{pointer}/edges",
                        cycle=cycle,
                    )
                visiting.add(node)
                for edge in edges_by_source.get(node, []):
                    visit(edge.to, [*trail, node])
                visiting.discard(node)
                done.add(node)

            visit(flow.start, [])


def _last_agent(node: PlanNode, project: LoadedProject) -> str | None:
    """The plan subtree's terminal agent, or None."""
    if isinstance(node, LeafNode):
        return node.name if node.kind == "agent" else None
    if isinstance(node, SequentialPlan):
        for step in reversed(node.steps):
            found = _last_agent(step, project)
            if found is not None:
                return found
        return None
    if isinstance(node, ParallelPlan):
        for step in reversed(node.then):
            found = _last_agent(step, project)
            if found is not None:
                return found
        if node.join is not None and node.join.kind == "agent":
            return node.join.name
        for branch in reversed(node.branches):
            found = _last_agent(branch, project)
            if found is not None:
                return found
        return None
    if isinstance(node, SupervisorPlan):
        return node.supervisor
    # GraphPlan: the last-declared agent with an edge to END, else any agent.
    last: str | None = None
    for source, edges in node.edges_by_source.items():
        if source in project.agents and any(
            edge.to == END_SENTINEL for edge in edges
        ):
            last = source
    if last is not None:
        return last
    for leaf in node.nodes:
        if leaf.kind == "agent":
            last = leaf.name
    return last


def plan_flow(project: LoadedProject, system_file: Path) -> FlowPlan:
    """Compile + validate the project's flow into an executable plan."""
    flow = project.system.flow
    builder = _PlanBuilder(project, system_file)
    root = builder.compile_flow(flow, name="", pointer="/flow", depth=0)

    primary = _last_agent(root, project)
    if primary is None:
        raise CompileError(
            "the flow contains no agent step; every flow needs at least "
            "one agent (function-only pipelines are not a v1 shape)",
            context={"file": str(system_file), "pointer": "/flow"},
        )

    # Legacy Phase 2c contract: a sequential flow's product is its final
    # state. Preserved for un-nested top-level sequential flows.
    steps: tuple[str, ...] = ()
    if isinstance(root, SequentialPlan) and all(
        isinstance(step, LeafNode) for step in root.steps
    ):
        steps = tuple(
            step.name for step in root.steps if isinstance(step, LeafNode)
        )

    return FlowPlan(
        pattern=flow.type,
        root=root,
        primary_agent=primary,
        agents=tuple(builder.agents_in_order),
        subflow_names=tuple(builder.subflow_names),
        steps=steps,
    )


__all__ = [
    "END_SENTINEL",
    "RESERVED_SUBNODE_SUFFIXES",
    "SUPPORTED_PATTERNS",
    "CompiledEdge",
    "FlowPlan",
    "GraphPlan",
    "LeafNode",
    "ParallelPlan",
    "PlanNode",
    "SequentialPlan",
    "SupervisorPlan",
    "flow_node_refs",
    "plan_flow",
    "validate_flow_refs",
    "validate_namespace",
]
