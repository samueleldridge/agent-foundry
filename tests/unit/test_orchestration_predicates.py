"""Predicate sandbox (docs/30 § Predicate language): AST validation +
restricted evaluation. Forbidden constructs raise CompileError with
line/column (Phase 7 exit gate)."""

from __future__ import annotations

import pytest

from foundry.core.errors import CompileError, OrchestrationError
from foundry.orchestration.predicates import StateProxy, compile_predicate

# --- allowed constructs -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("source", "state", "expected"),
    [
        ("state.severity == 'low'", {"severity": "low"}, True),
        ("state.severity in ['low', 'medium']", {"severity": "high"}, False),
        ("state.n >= 0.7 and not state.done", {"n": 0.9, "done": False}, True),
        ("state.a or state.b", {"a": False, "b": True}, True),
        ("len(state.breaks) > 0", {"breaks": [1, 2]}, True),
        ("isinstance(state.x, str)", {"x": "s"}, True),
        ("isinstance(state.x, float)", {"x": "s"}, False),
        ("state.info.sub == 'x'", {"info": {"sub": "x"}}, True),
        ("state.items[0] == 'first'", {"items": ["first"]}, True),
        ("state['key'] == 1", {"key": 1}, True),
        ("state.n == -1", {"n": -1}, True),
        ("state.v is None", {"v": None}, True),
        ("state.v is not None", {"v": None}, False),
        ("bool(state.flag)", {"flag": 1}, True),
        ("int(state.s) > 3", {"s": "5"}, True),
    ],
)
def test_allowed_predicates_evaluate(
    source: str, state: dict, expected: bool
) -> None:
    predicate = compile_predicate(source)
    assert predicate.evaluate(state) is expected


@pytest.mark.unit
def test_state_field_references_are_recorded_and_validated() -> None:
    predicate = compile_predicate(
        "state.severity == 'low' and len(state.breaks) > 0",
        state_fields={"severity", "breaks"},
    )
    assert set(predicate.state_fields) == {"severity", "breaks"}
    with pytest.raises(CompileError, match="unknown state field"):
        compile_predicate("state.ghost == 1", state_fields={"severity"})


# --- forbidden constructs ------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('rm -rf /')",   # import
        "open('/etc/passwd')",                    # non-whitelisted call
        "[x for x in state.a]",                   # comprehension
        "{x for x in state.a}",                   # set comprehension
        "lambda: 1",                              # lambda
        "state.a.append(1)",                      # mutation via method call
        "state.a + foo",                          # unknown name
        "(1).__class__",                          # attribute not on state
        "f'{state.a}'",                           # f-string
        "{'a': 1}",                               # dict display
        "state.a if state.b else state.c",        # conditional expression
        "state.a + 1",                            # arithmetic (non-pure gate)
        "len(state.a, state.b)",                  # ok call, but len(x, y)
        "isinstance(state.a, __import__)",        # bad isinstance type
        "getattr(state, 'x')",                    # reflection
        "state := 1",                             # walrus
        "b'bytes' == state.a",                    # bytes literal
        # Dunder reflection (Phase 7 review finding 3): each of these is
        # rooted at `state`, so the root check alone let them compile AND
        # evaluate (__class__ resolves on the proxy's TYPE, sidestepping
        # __getattr__). Any dunder anywhere in the chain is forbidden.
        "state.__class__ is not None",            # bare dunder
        "state.__class__.__mro__[1] is not None", # nested dunder chain
        "state.__dict__ is not None",             # instance dict
        "state.__init__.__globals__ is None",     # globals escape chain
        "len(state.__class__.__mro__) > 0",       # dunder inside a call
        "state.__slots__ == ()",                  # slots reflection
        "state.field.__class__ is not None",      # dunder mid-chain
        "state['k'].__class__ is not None",       # dunder after subscript
        # Single-underscore internals (Phase 8 pre-work): `state._data` is
        # the proxy's own slot — it hands back the RAW state dict,
        # sidestepping the read-only projection. Forbidden anywhere in a
        # chain, same as dunders.
        "state._data is not None",                # the proxy's slot itself
        "state._data['k'] == 1",                  # raw-dict subscript escape
        "state.field._private == 1",              # underscore mid-chain
        "state['k']._data is not None",           # underscore after subscript
        "len(state._data) > 0",                   # underscore inside a call
        "state._data.__class__ is not None",      # underscore then dunder
    ],
)
def test_forbidden_constructs_raise_compile_error(source: str) -> None:
    with pytest.raises(CompileError) as excinfo:
        compile_predicate(source, where="system.yaml", pointer="/flow/when")
    context = excinfo.value.context
    assert context.get("file") == "system.yaml"
    # line/column always present for AST violations (syntax errors carry
    # them in the message instead).
    if "construct" in context:
        assert isinstance(context["line"], int)
        assert isinstance(context["column"], int)


@pytest.mark.unit
def test_syntax_error_is_compile_error() -> None:
    with pytest.raises(CompileError, match="not a valid expression"):
        compile_predicate("state.a ==")


@pytest.mark.unit
def test_dunder_reflection_names_the_attribute() -> None:
    """The dunder refusal is specific (Phase 7 review finding 3): the
    error names the offending attribute, not just 'forbidden'."""
    with pytest.raises(CompileError, match="dunder attribute '__class__'"):
        compile_predicate("state.__class__ is not None")
    with pytest.raises(CompileError, match="dunder attribute '__mro__'"):
        compile_predicate("state.__class__.__mro__[1] is not None")


@pytest.mark.unit
def test_single_underscore_attribute_names_the_attribute() -> None:
    """`state._data` would return the proxy's raw dict (its __slots__
    entry, resolved before __getattr__ ever runs) — the refusal names the
    attribute so the operator sees exactly what tripped (Phase 8 pre-work)."""
    with pytest.raises(
        CompileError, match="underscore-leading attribute '_data'"
    ):
        compile_predicate("state._data is not None")
    with pytest.raises(
        CompileError, match="underscore-leading attribute '_private'"
    ):
        compile_predicate("state.field._private == 1")


# --- runtime behaviour -----------------------------------------------------------


@pytest.mark.unit
def test_missing_field_raises_orchestration_error_with_predicate_text() -> None:
    predicate = compile_predicate("state.missing == 1")
    with pytest.raises(OrchestrationError) as excinfo:
        predicate.evaluate({"present": 1})
    assert "state.missing == 1" in str(excinfo.value)
    assert excinfo.value.context["missing_field"] == "missing"


@pytest.mark.unit
def test_state_proxy_is_read_only() -> None:
    proxy = StateProxy({"a": 1})
    with pytest.raises(TypeError, match="read-only"):
        proxy.a = 2  # type: ignore[misc]


@pytest.mark.unit
def test_evaluation_has_no_builtins() -> None:
    """The eval globals expose ONLY the whitelist — even if a hostile
    string slipped past the AST walk, builtins are empty."""
    predicate = compile_predicate("len(state.a) == 1")
    assert "len" in predicate.code.co_names
    assert predicate.evaluate({"a": [1]}) is True
