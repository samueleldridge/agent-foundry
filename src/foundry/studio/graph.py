"""Graph export: compile_project → FlowPlan → GraphExport JSON (docs/72 §
Flow-graph visualisation — the schema there is normative).

The export walks the compiled :class:`FlowPlan` tree — the frontend does
layout + rendering only and never re-implements flow semantics. Pattern
mapping mirrors FlowPlan construction: ``single`` → start → agent → end;
``sequential`` → a chain of sequential edges; ``parallel`` → fan-out
``parallel`` edges + fan-in ``join`` edges; ``supervisor`` → bidirectional
``handoff`` edges to each worker; ``graph`` → the declared edges,
``conditional`` (edge label = predicate source) where a predicate exists.
Nested flows become ``group`` containers.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from foundry.core.errors import ConfigError, FoundryError
from foundry.orchestration.patterns import (
    END_SENTINEL,
    GraphPlan,
    LeafNode,
    ParallelPlan,
    PlanNode,
    SequentialPlan,
    SupervisorPlan,
)
from foundry.runtime.compiled import CompiledProject
from foundry.studio.context import StudioContext
from foundry.studio.schemas import (
    AgentSummary,
    FunctionSummary,
    GraphEdge,
    GraphExport,
    GraphNode,
    ValidationIssue,
    ValidationResult,
)

START = "__start__"
END = "__end__"

_Role = str | None


class _GraphBuilder:
    def __init__(self, compiled: CompiledProject) -> None:
        self.compiled = compiled
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.groups: list[str] = []

    # --- node helpers ---------------------------------------------------------------

    def _agent_summary(self, name: str) -> AgentSummary:
        agent = self.compiled.agent_map()[name]
        spec = agent.loaded.spec
        system = self.compiled.project.system
        views = self.compiled.compiled_state.agent_views
        view = views.get(name)
        return AgentSummary(
            model_binding=(
                f"{spec.model_binding.provider}/{spec.model_binding.model}"
            ),
            prompt_version=spec.prompt.version,
            tools=[
                (
                    f"{system.tools[tool].ref}@{system.tools[tool].version}"
                    if tool in system.tools
                    else tool
                )
                for tool in spec.tools
            ],
            state_read=(
                list(view.read) if view else list(spec.state_visibility.read)
            ),
            state_write=(
                list(view.write)
                if view
                else list(spec.state_visibility.write)
            ),
        )

    def _function_summary(self, name: str) -> FunctionSummary:
        function = self.compiled.functions.get(name)
        spec = function.spec if function else None
        return FunctionSummary(
            version=function.node_version if function else "",
            state_read=list(spec.state_visibility.read) if spec else [],
            state_write=list(spec.state_visibility.write) if spec else [],
        )

    def add_leaf(
        self, leaf: LeafNode, role: _Role, group: str | None
    ) -> str:
        if leaf.name not in self.nodes:
            self.nodes[leaf.name] = GraphNode(
                id=leaf.name,
                kind=leaf.kind,
                role=role,  # type: ignore[arg-type]
                label=leaf.name,
                group=group,
                agent=(
                    self._agent_summary(leaf.name)
                    if leaf.kind == "agent"
                    else None
                ),
                function=(
                    self._function_summary(leaf.name)
                    if leaf.kind == "function"
                    else None
                ),
            )
        return leaf.name

    def add_edge(
        self,
        source: str,
        target: str,
        kind: str,
        *,
        label: str | None = None,
        bidirectional: bool = False,
    ) -> None:
        self.edges.append(
            GraphEdge(
                id=f"e{len(self.edges)}",
                source=source,
                target=target,
                kind=kind,  # type: ignore[arg-type]
                label=label,
                bidirectional=bidirectional,
            )
        )

    def connect(self, sources: list[str], targets: list[str]) -> None:
        """Chain step boundaries: fan-out gets ``parallel`` edges, fan-in
        gets ``join`` edges, one-to-one is ``sequential``."""
        for source in sources:
            for target in targets:
                if len(targets) > 1:
                    kind = "parallel"
                elif len(sources) > 1:
                    kind = "join"
                else:
                    kind = "sequential"
                self.add_edge(source, target, kind)

    # --- the walk -------------------------------------------------------------------

    def walk(
        self, node: PlanNode, *, role: _Role, group: str | None
    ) -> tuple[list[str], list[str]]:
        """Emit ``node``'s subgraph; returns (entry ids, exit ids). An
        empty exit list means the subtree wires its own terminal edges
        (the graph pattern)."""
        if isinstance(node, LeafNode):
            name = self.add_leaf(node, role, group)
            return [name], [name]
        if isinstance(node, SequentialPlan):
            sub_group = group
            if node.name:
                sub_group = node.name
                if node.name not in self.groups:
                    self.groups.append(node.name)
            entries: list[str] = []
            exits: list[str] = []
            for step in node.steps:
                step_entries, step_exits = self.walk(
                    step, role=role if role is not None else "step",
                    group=sub_group,
                )
                if not entries:
                    entries = step_entries
                elif exits:
                    self.connect(exits, step_entries)
                exits = step_exits
            return entries, exits
        if isinstance(node, ParallelPlan):
            sub_group = group
            if node.name:
                sub_group = node.name
                if node.name not in self.groups:
                    self.groups.append(node.name)
            branch_entries: list[str] = []
            branch_exits: list[str] = []
            for branch in node.branches:
                walked_entries, walked_exits = self.walk(
                    branch, role="branch", group=sub_group
                )
                branch_entries.extend(walked_entries)
                branch_exits.extend(walked_exits)
            exits = branch_exits
            if node.join is not None:
                join_id = self.add_leaf(node.join, "join", sub_group)
                for source in branch_exits:
                    self.add_edge(source, join_id, "join")
                exits = [join_id]
            for step in node.then:
                step_entries, step_exits = self.walk(
                    step, role="step", group=sub_group
                )
                self.connect(exits, step_entries)
                exits = step_exits
            return branch_entries, exits
        if isinstance(node, SupervisorPlan):
            sub_group = group
            if node.name:
                sub_group = node.name
                if node.name not in self.groups:
                    self.groups.append(node.name)
            supervisor_id = self.add_leaf(
                LeafNode(kind="agent", name=node.supervisor),
                "supervisor",
                sub_group,
            )
            for target_name, worker_plan in node.workers:
                worker_entries, _worker_exits = self.walk(
                    worker_plan, role="worker", group=sub_group
                )
                for entry in worker_entries:
                    self.add_edge(
                        supervisor_id,
                        entry,
                        "handoff",
                        bidirectional=True,
                    )
                if target_name in node.workers_may_end:
                    for entry in worker_entries:
                        self.add_edge(entry, END, "sequential")
            return [supervisor_id], [supervisor_id]
        if isinstance(node, GraphPlan):
            for leaf in node.nodes:
                self.add_leaf(leaf, None, group)
            for source, compiled_edges in node.edges_by_source.items():
                for edge in compiled_edges:
                    target = END if edge.to == END_SENTINEL else edge.to
                    if edge.predicate is not None:
                        self.add_edge(
                            source,
                            target,
                            "conditional",
                            label=edge.predicate.source,
                        )
                    else:
                        self.add_edge(source, target, "sequential")
            return [node.start], []
        raise ConfigError(  # pragma: no cover - PlanNode union is closed
            f"unknown plan node {type(node).__name__}", context={}
        )


def export_graph(compiled: CompiledProject, project: str) -> GraphExport:
    plan = compiled.flow_plan()
    builder = _GraphBuilder(compiled)
    builder.nodes[START] = GraphNode(
        id=START, kind="start", role=None, label="start", group=None
    )
    root_role: _Role = "single" if plan.pattern == "single" else None
    entries, exits = builder.walk(plan.root, role=root_role, group=None)
    builder.nodes[END] = GraphNode(
        id=END, kind="end", role=None, label="end", group=None
    )
    builder.connect([START], entries)
    if exits:
        builder.connect(exits, [END])
    # Node order: __start__, flow nodes in first-appearance order, __end__.
    ordered = [builder.nodes[START]]
    ordered.extend(
        node
        for node_id, node in builder.nodes.items()
        if node_id not in (START, END)
    )
    ordered.append(builder.nodes[END])
    return GraphExport(
        project=project,
        system_version=compiled.system_version,
        pattern=plan.pattern,
        primary_agent=plan.primary_agent,
        nodes=ordered,
        edges=builder.edges,
        groups=builder.groups,
    )


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{name}/graph")
    def graph(name: str) -> object:
        ctx.project_dir(name)  # 404 for unknown projects
        try:
            compiled = ctx.compiled(name)
        except FoundryError as exc:
            # A project that does not COMPILE is a 422 with the structured
            # ValidationResult (docs/72 § Failure modes).
            context = exc.context
            line = context.get("line")
            column = context.get("column")
            issue = ValidationIssue(
                severity="error",
                message=str(exc),
                pointer=(
                    str(context["pointer"])
                    if context.get("pointer")
                    else None
                ),
                line=line if isinstance(line, int) else None,
                column=column if isinstance(column, int) else None,
                hint=(
                    str(context["hint"]) if context.get("hint") else None
                ),
            )
            return JSONResponse(
                status_code=422,
                content=ValidationResult(
                    ok=False, issues=[issue], kind="project"
                ).model_dump(mode="json"),
            )
        return export_graph(compiled, name)

    return router


__all__ = ["build_router", "export_graph"]
