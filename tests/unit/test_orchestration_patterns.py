"""Flow planning (docs/30): Phase 3 executes single + one-agent sequential;
the multi-agent patterns are stubbed with structured Phase 7 errors."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from foundry.config import (
    GraphFlow,
    ParallelFlow,
    SequentialFlow,
    SingleFlow,
    SupervisorFlow,
)
from foundry.core.errors import CompileError
from foundry.orchestration.patterns import ExecutionPlan, plan_flow

SYSTEM_FILE = Path("/proj/system.yaml")


def _project(flow: Any, agents: list[str], functions: list[str]) -> Any:
    """plan_flow consumes only .system.flow/.agents/.functions and .agents —
    a duck-typed stand-in keeps the unit test free of full project loading."""
    return SimpleNamespace(
        system=SimpleNamespace(flow=flow, agents=agents, functions=functions),
        agents=dict.fromkeys(agents),
        functions=dict.fromkeys(functions),
    )


@pytest.mark.unit
def test_single_flow_plans_one_agent() -> None:
    project = _project(SingleFlow(agent="hello_agent"), ["hello_agent"], [])
    assert plan_flow(project, SYSTEM_FILE) == ExecutionPlan(
        pattern="single", agent_name="hello_agent", steps=()
    )


@pytest.mark.unit
def test_sequential_fn_agent_fn_keeps_working() -> None:
    """The Phase 2c shape: [function, agent, function] stays executable."""
    project = _project(
        SequentialFlow(steps=["normalize", "hello_agent", "format"]),
        ["hello_agent"],
        ["normalize", "format"],
    )
    plan = plan_flow(project, SYSTEM_FILE)
    assert plan.pattern == "sequential"
    assert plan.agent_name == "hello_agent"
    assert plan.steps == ("normalize", "hello_agent", "format")


@pytest.mark.unit
@pytest.mark.parametrize(
    "flow",
    [
        ParallelFlow(parallel_branches=["a", "b"], join="c"),
        SupervisorFlow(supervisor="boss", workers=["a", "b"]),
        GraphFlow.model_validate(
            {"type": "graph", "start": "a",
             "edges": [{"from": "a", "to": "END"}]}
        ),
    ],
    ids=["parallel", "supervisor", "graph"],
)
def test_multi_agent_patterns_are_phase_7_stubs(flow: Any) -> None:
    project = _project(flow, ["a", "b", "c", "boss"], [])
    with pytest.raises(CompileError) as excinfo:
        plan_flow(project, SYSTEM_FILE)
    message = str(excinfo.value)
    assert "Phase 7" in message
    assert excinfo.value.context["pointer"] == "/flow/type"
    assert excinfo.value.context["received"] == flow.type


@pytest.mark.unit
@pytest.mark.parametrize("agents", [[], ["a1", "a2"]], ids=["zero", "two"])
def test_sequential_without_exactly_one_agent_is_phase_7(
    agents: list[str],
) -> None:
    project = _project(
        SequentialFlow(steps=[*agents, "fn"]), agents, ["fn"]
    )
    with pytest.raises(CompileError) as excinfo:
        plan_flow(project, SYSTEM_FILE)
    assert "Phase 7" in str(excinfo.value)
    assert excinfo.value.context["pointer"] == "/flow/steps"
    assert excinfo.value.context["agent_steps"] == agents


@pytest.mark.unit
def test_single_flow_agent_must_be_an_agent() -> None:
    project = _project(SingleFlow(agent="fn"), [], ["fn"])
    with pytest.raises(CompileError) as excinfo:
        plan_flow(project, SYSTEM_FILE)
    assert excinfo.value.context["pointer"] == "/flow/agent"
