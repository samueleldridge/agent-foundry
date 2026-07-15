"""Graph export: FlowPlan → GraphExport per pattern (docs/72 § Flow-graph
visualisation — the schema is normative)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.orchestration.compiler import compile_project
from foundry.studio.graph import export_graph

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


@pytest.mark.unit
def test_single_pattern_export_matches_docs72_shape() -> None:
    compiled = compile_project(REPO_ROOT / "projects" / "hello")
    export = export_graph(compiled, "hello")
    assert export.pattern == "single"
    assert export.primary_agent == "hello_agent"
    assert [node.id for node in export.nodes] == [
        "__start__",
        "hello_agent",
        "__end__",
    ]
    agent_node = export.nodes[1]
    assert agent_node.kind == "agent"
    assert agent_node.role == "single"
    assert agent_node.agent is not None
    assert agent_node.agent.model_binding == "anthropic/claude-haiku-4-5"
    assert agent_node.agent.prompt_version == "v2"
    assert agent_node.agent.tools == ["catalog/http_get_json@v1"]
    assert agent_node.agent.state_read == ["name"]
    assert agent_node.agent.state_write == ["greeting"]
    assert [
        (edge.source, edge.target, edge.kind) for edge in export.edges
    ] == [
        ("__start__", "hello_agent", "sequential"),
        ("hello_agent", "__end__", "sequential"),
    ]
    assert export.groups == []
    assert export.system_version == compiled.system_version


@pytest.mark.unit
def test_supervisor_pattern_export_bidirectional_handoffs() -> None:
    compiled = compile_project(REPO_ROOT / "projects" / "team_hello")
    export = export_graph(compiled, "team_hello")
    assert export.pattern == "supervisor"
    assert export.primary_agent == "coordinator"
    nodes = {node.id: node for node in export.nodes}
    assert nodes["coordinator"].role == "supervisor"
    assert nodes["drafter"].role == "worker"
    assert nodes["publisher"].role == "worker"
    assert nodes["publisher"].agent is not None
    assert nodes["publisher"].agent.tools == ["local/publish_greeting@v1"]
    handoffs = {
        (edge.source, edge.target): edge
        for edge in export.edges
        if edge.kind == "handoff"
    }
    assert set(handoffs) == {
        ("coordinator", "drafter"),
        ("coordinator", "publisher"),
    }
    assert all(edge.bidirectional for edge in handoffs.values())
    sequential = [
        (edge.source, edge.target)
        for edge in export.edges
        if edge.kind == "sequential"
    ]
    assert ("__start__", "coordinator") in sequential
    assert ("coordinator", "__end__") in sequential


@pytest.mark.unit
def test_graph_pattern_conditional_edges_carry_predicate_source() -> None:
    """A synthetic GraphPlan walk: conditional edges get the predicate
    source as the label; END maps to __end__ (docs/72 pattern mapping)."""
    from foundry.orchestration.patterns import (
        CompiledEdge,
        FlowPlan,
        GraphPlan,
        LeafNode,
    )
    from foundry.orchestration.predicates import compile_predicate
    from foundry.studio.graph import _GraphBuilder

    compiled = compile_project(REPO_ROOT / "projects" / "hello")
    predicate = compile_predicate(
        "state.greeting != ''",
        state_fields={"name", "greeting"},
        where="test",
        pointer="/flow/edges/0/when",
    )
    plan = GraphPlan(
        name="",
        start="hello_agent",
        nodes=(LeafNode(kind="agent", name="hello_agent"),),
        edges_by_source={
            "hello_agent": (
                CompiledEdge(to="END", predicate=predicate),
                CompiledEdge(to="END", predicate=None),
            )
        },
    )
    flow_plan = FlowPlan(
        pattern="graph",
        root=plan,
        primary_agent="hello_agent",
        agents=("hello_agent",),
        subflow_names=(),
    )
    builder = _GraphBuilder(compiled)
    entries, exits = builder.walk(flow_plan.root, role=None, group=None)
    assert entries == ["hello_agent"]
    assert exits == []  # the graph pattern wires its own terminal edges
    kinds = [(edge.kind, edge.label, edge.target) for edge in builder.edges]
    assert kinds == [
        ("conditional", "state.greeting != ''", "__end__"),
        ("sequential", None, "__end__"),
    ]


@pytest.mark.unit
def test_sequential_and_parallel_walk_shapes() -> None:
    """Synthetic sequential + parallel plans produce chain / fan-out +
    fan-in edges (docs/72 pattern mapping)."""
    from foundry.orchestration.patterns import (
        LeafNode,
        ParallelPlan,
        SequentialPlan,
    )
    from foundry.studio.graph import _GraphBuilder

    compiled = compile_project(REPO_ROOT / "projects" / "team_hello")
    agents = ["coordinator", "drafter", "publisher"]

    seq = SequentialPlan(
        name="",
        steps=tuple(LeafNode(kind="agent", name=name) for name in agents),
    )
    builder = _GraphBuilder(compiled)
    entries, exits = builder.walk(seq, role=None, group=None)
    assert entries == ["coordinator"]
    assert exits == ["publisher"]
    assert [(e.source, e.target, e.kind) for e in builder.edges] == [
        ("coordinator", "drafter", "sequential"),
        ("drafter", "publisher", "sequential"),
    ]
    assert all(node.role == "step" for node in builder.nodes.values())

    par = ParallelPlan(
        name="",
        branches=(
            LeafNode(kind="agent", name="drafter"),
            LeafNode(kind="agent", name="publisher"),
        ),
        join=LeafNode(kind="agent", name="coordinator"),
        then=(),
    )
    builder = _GraphBuilder(compiled)
    entries, exits = builder.walk(par, role=None, group=None)
    assert set(entries) == {"drafter", "publisher"}
    assert exits == ["coordinator"]
    join_edges = [
        (e.source, e.target) for e in builder.edges if e.kind == "join"
    ]
    assert set(join_edges) == {
        ("drafter", "coordinator"),
        ("publisher", "coordinator"),
    }
    assert builder.nodes["coordinator"].role == "join"
    assert builder.nodes["drafter"].role == "branch"
