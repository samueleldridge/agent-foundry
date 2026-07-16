"""Chat text → run input helpers (docs/72 § Chat UX): the single-field
auto-wrap stays exact; multi-field projects get a ready-to-fill JSON
template in the structured error; composer field metadata derives from
the project input model."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from foundry.core.errors import (
    ConfigValidationError,
    ProjectUnavailableError,
)
from foundry.studio.app import _studio_status_for
from foundry.studio.chat import _input_fields, _input_from_text


class SingleInput(BaseModel):
    request: str


class TeamInput(BaseModel):
    request: str
    audience: str
    tone: str = "friendly"


class TypedInput(BaseModel):
    request: str
    limit: int
    tags: list[str]
    strict: bool


@pytest.mark.unit
def test_plain_text_wraps_single_required_field() -> None:
    assert _input_from_text("hi there", SingleInput) == {
        "request": "hi there"
    }


@pytest.mark.unit
def test_json_object_is_the_input_verbatim() -> None:
    assert _input_from_text(
        '{"request": "ship it", "audience": "the team"}', TeamInput
    ) == {"request": "ship it", "audience": "the team"}


@pytest.mark.unit
def test_multi_field_error_carries_ready_to_fill_template() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        _input_from_text("hi team", TeamInput)
    err = excinfo.value
    assert err.context["required_fields"] == ["request", "audience"]
    assert err.context["template"] == {"request": "...", "audience": "..."}
    # The message itself contains the copy-pasteable template.
    assert '{"request": "...", "audience": "..."}' in str(err)


@pytest.mark.unit
def test_template_placeholders_follow_field_types() -> None:
    with pytest.raises(ConfigValidationError) as excinfo:
        _input_from_text("hello", TypedInput)
    assert excinfo.value.context["template"] == {
        "request": "...",
        "limit": 0,
        "tags": [],
        "strict": False,
    }


@pytest.mark.unit
def test_input_fields_metadata_names_types_and_requiredness() -> None:
    fields = {f.name: f for f in _input_fields(TeamInput)}
    assert set(fields) == {"request", "audience", "tone"}
    assert fields["request"].type == "string"
    assert fields["request"].required is True
    assert fields["tone"].required is False


@pytest.mark.unit
def test_input_fields_excludes_the_auto_threaded_turns_field() -> None:
    class TurnsInput(BaseModel):
        request: str
        turns: list[dict[str, str]] = []

    assert [f.name for f in _input_fields(TurnsInput)] == ["request"]


@pytest.mark.unit
def test_project_unavailable_error_maps_to_424_with_full_context() -> None:
    err = ProjectUnavailableError(
        "project 'rag_hello' is unavailable",
        project="rag_hello",
        env_vars=["COHERE_API_KEY"],
        remedy="set COHERE_API_KEY and restart foundry studio",
    )
    assert _studio_status_for(err) == 424
    body = err.to_dict()
    assert body["error_class"] == "ProjectUnavailableError"
    assert body["context"]["project"] == "rag_hello"
    assert body["context"]["env_vars"] == ["COHERE_API_KEY"]
    assert "COHERE_API_KEY" in body["context"]["remedy"]
