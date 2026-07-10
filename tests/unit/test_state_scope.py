"""State compilation + structural visibility (docs/22)."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import get_type_hints

import pytest

from foundry.config import FieldSpec, StateSpec, StateVisibility
from foundry.core import FoundryMessage, Reducer
from foundry.core.errors import ConfigError, StateVisibilityError
from foundry.orchestration import compile_state, parse_type_string


def _spec(**overrides: object) -> StateSpec:
    base: dict[str, object] = {
        "schema": {
            "messages": {"type": "list[FoundryMessage]"},
            "current_case": {"type": "str"},
            "draft_plan": {"type": "str | None", "default": None},
            "scratchpad": {"type": "dict[str, Any]", "default": {}},
        },
        "reducers": {"messages": "append", "scratchpad": "merge"},
        "visibility": {
            "reader": {"read": ["messages", "current_case"], "write": ["messages"]},
            "planner": {"read": ["current_case"], "write": ["draft_plan"]},
        },
    }
    base.update(overrides)
    return StateSpec.model_validate(base)


# --- type parsing ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("str", str),
        ("int", int),
        ("Decimal", Decimal),
        ("datetime", datetime),
        ("FoundryMessage", FoundryMessage),
        ("list[int]", list[int]),
        ("dict[str, int]", dict[str, int]),
        ("set[str]", set[str]),
        ("list[dict[str, int]]", list[dict[str, int]]),
        ("str | None", str | None),
    ],
)
def test_parse_type_string_supported(type_str: str, expected: object) -> None:
    assert parse_type_string(type_str) == expected


@pytest.mark.unit
def test_parse_user_pydantic_ref() -> None:
    cls = parse_type_string("BaseModel:foundry.core.messages:TextBlock")
    from foundry.core import TextBlock

    assert cls is TextBlock


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "float64",
        "list[str, int]",
        "int | str",
        "BaseModel:not.a.module:Thing",
        "BaseModel:foundry.core.messages:Missing",
        "Callable[[], None]",
    ],
)
def test_parse_type_string_unparseable_raises_config_error(bad: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_type_string(bad)
    assert bad.split("[")[0].split(":")[0] in str(excinfo.value)


# --- compilation ---------------------------------------------------------------


@pytest.mark.unit
def test_compile_builds_model_with_reducer_metadata() -> None:
    compiled = compile_state(_spec(), ["reader", "planner"])
    assert compiled.model.reducer_map() == {
        "messages": Reducer.APPEND,
        "current_case": Reducer.LAST_WRITE_WINS,
        "draft_plan": Reducer.LAST_WRITE_WINS,
        "scratchpad": Reducer.MERGE,
    }
    assert compiled.reducers["scratchpad"] is Reducer.MERGE


@pytest.mark.unit
def test_state_model_json_round_trip() -> None:
    compiled = compile_state(_spec(), ["reader", "planner"])
    instance = compiled.model.model_validate(
        {"messages": [], "current_case": "case-1", "scratchpad": {"k": 1}}
    )
    dumped = json.loads(instance.model_dump_json())
    assert compiled.model.model_validate(dumped) == instance


@pytest.mark.unit
def test_agent_views_are_typed_dicts_over_declared_fields() -> None:
    compiled = compile_state(_spec(), ["reader", "planner"])
    reader = compiled.agent_views["reader"]
    assert set(get_type_hints(reader.input_type)) == {"messages", "current_case"}
    assert set(get_type_hints(reader.output_type)) == {"messages"}
    planner = compiled.agent_views["planner"]
    assert set(get_type_hints(planner.input_type)) == {"current_case"}


@pytest.mark.unit
def test_projection_omits_forbidden_fields_structurally() -> None:
    compiled = compile_state(_spec(), ["reader", "planner"])
    planner = compiled.agent_views["planner"]
    view = planner.project_input(
        {"messages": ["m"], "current_case": "c", "draft_plan": "secret"}
    )
    assert view == {"current_case": "c"}
    assert "draft_plan" not in view  # literally absent, not None


@pytest.mark.unit
def test_out_of_scope_write_raises_state_visibility_error() -> None:
    compiled = compile_state(_spec(), ["reader", "planner"])
    reader = compiled.agent_views["reader"]
    with pytest.raises(StateVisibilityError) as excinfo:
        reader.validate_writes({"draft_plan": "sneaky"})
    assert excinfo.value.context["forbidden_fields"] == ["draft_plan"]
    assert reader.validate_writes({"messages": ["ok"]}) == {"messages": ["ok"]}


@pytest.mark.unit
def test_missing_visibility_entry_fails_compile() -> None:
    with pytest.raises(StateVisibilityError) as excinfo:
        compile_state(_spec(), ["reader", "planner", "ghost_agent"])
    assert "ghost_agent" in str(excinfo.value)


@pytest.mark.unit
def test_visibility_referencing_unknown_field_fails_compile() -> None:
    spec = _spec(
        visibility={
            "reader": {"read": ["messages", "nonexistent"], "write": ["messages"]},
        }
    )
    with pytest.raises(StateVisibilityError) as excinfo:
        compile_state(spec, ["reader"])
    assert excinfo.value.context["unknown_fields"] == ["nonexistent"]


@pytest.mark.unit
def test_orphan_visibility_entry_fails_compile() -> None:
    with pytest.raises(StateVisibilityError) as excinfo:
        compile_state(_spec(), ["reader"])  # planner entry now orphaned
    assert "planner" in str(excinfo.value)


@pytest.mark.unit
def test_reducer_for_unknown_field_fails_compile() -> None:
    spec = _spec(reducers={"messages": "append", "ghost": "merge"})
    with pytest.raises(ConfigError) as excinfo:
        compile_state(spec, ["reader", "planner"])
    assert excinfo.value.context["unknown_fields"] == ["ghost"]


@pytest.mark.unit
def test_empty_visibility_rejected_at_schema_level() -> None:
    with pytest.raises(ValueError):
        StateVisibility.model_validate({"read": [], "write": []})


@pytest.mark.unit
def test_required_field_missing_fails_validation() -> None:
    compiled = compile_state(_spec(), ["reader", "planner"])
    with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError
        compiled.model.model_validate({"messages": []})  # current_case required


@pytest.mark.unit
def test_field_spec_defaults_apply() -> None:
    spec = StateSpec.model_validate(
        {
            "schema": {
                "count": {"type": "int", "default": 0},
                "note": {"type": "str | None"},
            },
            "visibility": {"a": {"read": ["count"], "write": ["note"]}},
        }
    )
    compiled = compile_state(spec, ["a"])
    instance = compiled.model.model_validate({})
    assert instance.count == 0  # type: ignore[attr-defined]
    assert instance.note is None  # type: ignore[attr-defined]


@pytest.mark.unit
def test_field_spec_roundtrip_model() -> None:
    field = FieldSpec(type="str", description="x")
    assert field.default is None


@pytest.mark.unit
def test_visibility_fuzz_projection_never_leaks() -> None:
    """Phase 7 risk-register item: 'state visibility enforcement has false
    negatives'. Seeded fuzz over random schemas + scopes: the projection an
    agent receives is EXACTLY its read scope intersected with the present
    state — never one field more."""
    import random

    rng = random.Random(2026_07)
    for round_number in range(50):
        n_fields = rng.randint(1, 12)
        fields = [f"f{i}" for i in range(n_fields)]
        read = sorted(rng.sample(fields, rng.randint(0, n_fields)))
        write = sorted(rng.sample(fields, rng.randint(0, n_fields)))
        if not read and not write:
            read = [fields[0]]
        spec = StateSpec.model_validate(
            {
                "schema": {name: {"type": "str | None"} for name in fields},
                "visibility": {"fuzzed": {"read": read, "write": write}},
            }
        )
        compiled = compile_state(spec, ["fuzzed"])
        view = compiled.agent_views["fuzzed"]
        present = {
            name: f"v-{name}"
            for name in rng.sample(fields, rng.randint(0, n_fields))
        }
        projection = view.project_input(present)
        assert set(projection) == set(read) & set(present), (
            f"round {round_number}: projection leaked "
            f"{set(projection) - set(read)}"
        )
        # Write-side: a delta touching any non-write field is refused.
        forbidden = sorted(set(fields) - set(write))
        if forbidden:
            with pytest.raises(StateVisibilityError):
                view.validate_writes({forbidden[0]: "x"})
