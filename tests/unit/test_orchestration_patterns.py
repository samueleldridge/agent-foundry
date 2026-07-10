"""Flow-plan compilation (docs/30): all five patterns + nesting (Phase 7)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.config import (
    FieldSpec,
    GraphFlow,
    Guardrails,
    ParallelFlow,
    SequentialFlow,
    SingleFlow,
    SupervisorFlow,
)
from foundry.core.errors import CompileError
from foundry.orchestration.patterns import (
    GraphPlan,
    LeafNode,
    ParallelPlan,
    SequentialPlan,
    SupervisorPlan,
    plan_flow,
    validate_namespace,
)

SYSTEM_FILE = Path("/proj/system.yaml")


def _project(
    flow: Any,
    agents: list[str],
    functions: list[str],
    *,
    state_fields: list[str] | None = None,
    guardrails: Guardrails | None = None,
) -> Any:
    """plan_flow consumes .system.flow/.agents/.functions/.guardrails,
    .agents, .functions and .state.state_schema — a duck-typed stand-in
    keeps the unit test free of full project loading."""
    return SimpleNamespace(
        system=SimpleNamespace(
            flow=flow,
            agents=agents,
            functions=functions,
            guardrails=guardrails or Guardrails(),
        ),
        agents=dict.fromkeys(agents),
        functions=dict.fromkeys(functions),
        state=SimpleNamespace(
            state_schema={
                name: FieldSpec(type="str")
                for name in (state_fields or ["done", "severity"])
            }
        ),
    )


# --- single / sequential (legacy shapes stay) ---------------------------------------


@pytest.mark.unit
def test_single_flow_plans_one_agent() -> None:
    project = _project(SingleFlow(agent="hello_agent"), ["hello_agent"], [])
    plan = plan_flow(project, SYSTEM_FILE)
    assert plan.pattern == "single"
    assert plan.root == LeafNode(kind="agent", name="hello_agent")
    assert plan.primary_agent == "hello_agent"
    assert plan.steps == ()


@pytest.mark.unit
def test_sequential_fn_agent_fn_keeps_working() -> None:
    """The Phase 2c shape: [function, agent, function] stays executable and
    keeps the legacy steps tuple (output = final state)."""
    project = _project(
        SequentialFlow(steps=["normalize", "hello_agent", "format"]),
        ["hello_agent"],
        ["normalize", "format"],
    )
    plan = plan_flow(project, SYSTEM_FILE)
    assert plan.pattern == "sequential"
    assert plan.primary_agent == "hello_agent"
    assert plan.steps == ("normalize", "hello_agent", "format")


@pytest.mark.unit
def test_multi_agent_sequential_plans() -> None:
    project = _project(
        SequentialFlow(steps=["classifier", "investigator", "recommender"]),
        ["classifier", "investigator", "recommender"],
        [],
    )
    plan = plan_flow(project, SYSTEM_FILE)
    assert isinstance(plan.root, SequentialPlan)
    assert plan.primary_agent == "recommender"  # last agent = output owner
    assert plan.agents == ("classifier", "investigator", "recommender")


@pytest.mark.unit
def test_single_flow_agent_must_be_an_agent() -> None:
    project = _project(SingleFlow(agent="fn"), [], ["fn"])
    with pytest.raises(CompileError) as excinfo:
        plan_flow(project, SYSTEM_FILE)
    assert excinfo.value.context["pointer"] == "/flow"


@pytest.mark.unit
def test_repeated_node_in_flow_is_compile_error() -> None:
    project = _project(
        SequentialFlow(steps=["a", "b", "a"]), ["a", "b"], []
    )
    with pytest.raises(CompileError, match="more than once"):
        plan_flow(project, SYSTEM_FILE)


# --- parallel ------------------------------------------------------------------------


@pytest.mark.unit
def test_parallel_plan_shape() -> None:
    project = _project(
        ParallelFlow(
            parallel_branches=["a", "b", "c"], join="agg", then=["final"]
        ),
        ["a", "b", "c", "agg", "final"],
        [],
    )
    plan = plan_flow(project, SYSTEM_FILE)
    root = plan.root
    assert isinstance(root, ParallelPlan)
    assert len(root.branches) == 3
    assert root.join == LeafNode(kind="agent", name="agg")
    assert plan.primary_agent == "final"  # then wins over join


# --- supervisor ------------------------------------------------------------------------


def _supervisor_project(**overrides: Any) -> Any:
    flow = SupervisorFlow.model_validate(
        {
            "type": "supervisor",
            "supervisor": "orch",
            "workers": ["detect", "resolve"],
            **overrides,
        }
    )
    return _project(flow, ["orch", "detect", "resolve"], [])


@pytest.mark.unit
def test_supervisor_plan_defaults() -> None:
    plan = plan_flow(_supervisor_project(), SYSTEM_FILE)
    root = plan.root
    assert isinstance(root, SupervisorPlan)
    assert root.supervisor == "orch"
    assert [t for t, _ in root.workers] == ["detect", "resolve"]
    # Default allowed_handoffs: supervisor → all workers + END.
    assert root.supervisor_targets == ("detect", "resolve", "END")
    # Default worker handoffs: back to the supervisor only.
    assert root.workers_may_end == frozenset()
    assert plan.primary_agent == "orch"


@pytest.mark.unit
def test_supervisor_allowed_handoffs_restrict_targets() -> None:
    plan = plan_flow(
        _supervisor_project(
            handoff_policy={
                "allowed_handoffs": {
                    "orch": ["detect", "END"],
                    "resolve": ["orch", "END"],
                }
            },
            termination={"when": "state.done == 'yes'"},
        ),
        SYSTEM_FILE,
    )
    root = plan.root
    assert isinstance(root, SupervisorPlan)
    assert root.supervisor_targets == ("detect", "END")
    assert root.workers_may_end == frozenset({"resolve"})
    assert root.termination_when is not None


@pytest.mark.unit
def test_supervisor_handoff_to_unknown_worker_is_compile_error() -> None:
    with pytest.raises(CompileError, match="unknown worker"):
        plan_flow(
            _supervisor_project(
                handoff_policy={"allowed_handoffs": {"orch": ["ghost"]}}
            ),
            SYSTEM_FILE,
        )


@pytest.mark.unit
def test_supervisor_rule_mode_is_deferred() -> None:
    with pytest.raises(CompileError, match="deferred"):
        plan_flow(
            _supervisor_project(handoff_policy={"mode": "rule"}), SYSTEM_FILE
        )


@pytest.mark.unit
def test_supervisor_force_return_false_is_deferred() -> None:
    with pytest.raises(CompileError, match="force_return_to_supervisor"):
        plan_flow(
            _supervisor_project(
                handoff_policy={"force_return_to_supervisor": False}
            ),
            SYSTEM_FILE,
        )


@pytest.mark.unit
def test_supervisor_must_be_an_agent() -> None:
    flow = SupervisorFlow.model_validate(
        {"type": "supervisor", "supervisor": "fn", "workers": ["a"]}
    )
    project = _project(flow, ["a"], ["fn"])
    with pytest.raises(CompileError, match="must be an AGENT"):
        plan_flow(project, SYSTEM_FILE)


@pytest.mark.unit
def test_supervisor_escalate_to_must_be_a_worker() -> None:
    with pytest.raises(CompileError, match="escalate_to"):
        plan_flow(
            _supervisor_project(
                termination={
                    "max_hops": 4,
                    "on_max_hops": "escalate",
                    "escalate_to": "ghost",
                }
            ),
            SYSTEM_FILE,
        )


@pytest.mark.unit
def test_supervisor_termination_predicate_validates_fields() -> None:
    with pytest.raises(CompileError, match="unknown state field"):
        plan_flow(
            _supervisor_project(
                termination={"when": "state.nope == 1"}
            ),
            SYSTEM_FILE,
        )


# --- graph -----------------------------------------------------------------------------


def _graph_project(edges: list[dict[str, Any]], **kw: Any) -> Any:
    flow = GraphFlow.model_validate(
        {"type": "graph", "start": "triage", "edges": edges, **kw}
    )
    names = {e["from"] for e in edges} | {
        e["to"] for e in edges if e["to"] != "END"
    }
    return _project(flow, sorted(names), [])


@pytest.mark.unit
def test_graph_plan_compiles_conditional_edges() -> None:
    project = _graph_project(
        [
            {"from": "triage", "to": "low", "when": "state.severity == 'low'"},
            {"from": "triage", "to": "high"},
            {"from": "low", "to": "END"},
            {"from": "high", "to": "END"},
        ]
    )
    plan = plan_flow(project, SYSTEM_FILE)
    root = plan.root
    assert isinstance(root, GraphPlan)
    assert root.start == "triage"
    triage_edges = root.edges_by_source["triage"]
    assert triage_edges[0].predicate is not None
    assert triage_edges[1].predicate is None  # the else edge


@pytest.mark.unit
def test_graph_unreachable_node_fails_compile() -> None:
    project = _graph_project(
        [
            {"from": "triage", "to": "END"},
            {"from": "island", "to": "END"},
        ]
    )
    with pytest.raises(CompileError, match=r"unreachable.*island"):
        plan_flow(project, SYSTEM_FILE)


@pytest.mark.unit
def test_graph_node_without_path_to_end_fails_compile() -> None:
    project = _graph_project(
        [
            {"from": "triage", "to": "stuck"},
            {"from": "triage", "to": "END"},
        ]
    )
    with pytest.raises(CompileError, match=r"no path to END.*stuck"):
        plan_flow(project, SYSTEM_FILE)


@pytest.mark.unit
def test_graph_cycle_requires_cycles_allowed() -> None:
    edges = [
        {"from": "triage", "to": "review"},
        {"from": "review", "to": "triage", "when": "state.done == 'no'"},
        {"from": "review", "to": "END"},
    ]
    with pytest.raises(CompileError, match="cycle"):
        plan_flow(_graph_project(edges), SYSTEM_FILE)
    plan = plan_flow(
        _graph_project(edges, cycles_allowed=True), SYSTEM_FILE
    )
    assert isinstance(plan.root, GraphPlan)


@pytest.mark.unit
def test_graph_end_is_a_sink() -> None:
    project = _graph_project(
        [
            {"from": "triage", "to": "END"},
            {"from": "END", "to": "triage"},
        ]
    )
    with pytest.raises(CompileError, match="sink"):
        plan_flow(project, SYSTEM_FILE)


@pytest.mark.unit
def test_graph_edge_predicate_ast_violation_is_compile_error() -> None:
    project = _graph_project(
        [
            {"from": "triage", "to": "END",
             "when": "__import__('os').system('x')"},
        ]
    )
    with pytest.raises(CompileError, match="forbidden construct"):
        plan_flow(project, SYSTEM_FILE)


# --- nesting --------------------------------------------------------------------------


@pytest.mark.unit
def test_supervisor_with_nested_parallel_worker() -> None:
    """docs/30 § Composition: a supervisor whose worker is an inline
    parallel group."""
    flow = SupervisorFlow.model_validate(
        {
            "type": "supervisor",
            "supervisor": "orch",
            "workers": [
                "detect",
                {
                    "investigation": {
                        "type": "parallel",
                        "parallel_branches": ["lookup_a", "lookup_b"],
                        "join": "aggregate",
                    }
                },
            ],
        }
    )
    project = _project(
        flow, ["orch", "detect", "lookup_a", "lookup_b", "aggregate"], []
    )
    plan = plan_flow(project, SYSTEM_FILE)
    root = plan.root
    assert isinstance(root, SupervisorPlan)
    targets = [t for t, _ in root.workers]
    assert targets == ["detect", "investigation"]
    nested = dict(root.workers)["investigation"]
    assert isinstance(nested, ParallelPlan)
    assert nested.name == "investigation"
    assert root.supervisor_targets == ("detect", "investigation", "END")
    assert plan.subflow_names == ("investigation",)


@pytest.mark.unit
def test_nested_name_collision_is_compile_error() -> None:
    flow = SupervisorFlow.model_validate(
        {
            "type": "supervisor",
            "supervisor": "orch",
            "workers": [
                {"detect": {"type": "single", "agent": "a"}},
            ],
        }
    )
    project = _project(flow, ["orch", "a", "detect"], [])
    with pytest.raises(CompileError, match="collides"):
        plan_flow(project, SYSTEM_FILE)


@pytest.mark.unit
def test_nested_graph_is_deferred() -> None:
    flow = SequentialFlow.model_validate(
        {
            "type": "sequential",
            "steps": [
                {
                    "sub": {
                        "type": "graph",
                        "start": "a",
                        "edges": [{"from": "a", "to": "END"}],
                    }
                }
            ],
        }
    )
    project = _project(flow, ["a"], [])
    with pytest.raises(CompileError, match="nested 'graph'"):
        plan_flow(project, SYSTEM_FILE)


@pytest.mark.unit
def test_nesting_depth_cap() -> None:
    flow = SequentialFlow.model_validate(
        {
            "type": "sequential",
            "steps": [
                {
                    "l1": {
                        "type": "sequential",
                        "steps": [
                            {
                                "l2": {
                                    "type": "sequential",
                                    "steps": ["a"],
                                }
                            }
                        ],
                    }
                }
            ],
        }
    )
    project = _project(
        flow, ["a"], [], guardrails=Guardrails(max_flow_nesting_depth=1)
    )
    with pytest.raises(CompileError, match="max_flow_nesting_depth"):
        plan_flow(project, SYSTEM_FILE)


# --- reserved sub-node names (Phase 3 review finding 4) -------------


@pytest.mark.unit
def test_function_named_like_reserved_subnode_is_compile_error() -> None:
    """The runtime expands agents into <agent>__llm/tools/finish/... sub-
    nodes; a function claiming one of those names must fail at COMPILE
    time (exit 2 via the CLI), not at runtime graph wiring."""
    project = _project(
        SequentialFlow(steps=["hello_agent", "hello_agent__llm"]),
        ["hello_agent"],
        ["hello_agent__llm"],
    )
    with pytest.raises(CompileError) as excinfo:
        validate_namespace(project, SYSTEM_FILE)
    message = str(excinfo.value)
    assert "hello_agent__llm" in message
    assert "reserved" in message
    assert excinfo.value.context["collisions"] == ["hello_agent__llm"]
    assert excinfo.value.context["file"] == str(SYSTEM_FILE)


@pytest.mark.unit
def test_non_reserved_double_underscore_names_are_fine() -> None:
    project = _project(
        SequentialFlow(steps=["hello_agent", "other__x"]),
        ["hello_agent"],
        ["other__x"],
    )
    validate_namespace(project, SYSTEM_FILE)  # no raise
