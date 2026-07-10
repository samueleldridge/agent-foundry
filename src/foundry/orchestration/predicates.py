"""The `when:` predicate language — sandboxed AST validation + evaluation.

Implements docs/30 § Predicate language. Used by ``graph.edges[].when`` and
``supervisor.termination.when``. A predicate is a restricted Python
expression over a ``state`` proxy:

Allowed: comparisons (``==`` ``!=`` ``<`` ``<=`` ``>`` ``>=`` ``in``
``not in`` ``is`` ``is not``), boolean ``and``/``or``/``not``, attribute
access rooted at ``state``, subscripts, literals (numbers, strings,
booleans, ``None``), list/tuple/set displays of allowed expressions, unary
minus, and whitelisted calls (``len``, ``isinstance``, ``bool``, ``str``,
``int``, ``float``).

Forbidden (``CompileError`` at compile time, with line/column): any other
function call, imports, lambdas, comprehensions, assignments/mutation,
f-strings, dict displays, walrus, starred args, keywords args, attribute
chains NOT rooted at ``state``, any dunder attribute anywhere in a chain
(``state.__class__.__mro__``-style reflection), and names outside the
whitelist.

Evaluation is ``eval`` over the validated AST with empty builtins and a
read-only state proxy; a missing state field surfaces as
``OrchestrationError`` carrying the predicate text (docs/30 § Failure
modes). No langgraph imports — this module is pure stdlib.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from types import CodeType
from typing import Any

from foundry.core.errors import CompileError, OrchestrationError

_ALLOWED_CALLS: dict[str, Any] = {
    "len": len,
    "isinstance": isinstance,
    "bool": bool,
    "str": str,
    "int": int,
    "float": float,
}

_ALLOWED_ISINSTANCE_TYPES = ("bool", "str", "int", "float", "list", "dict")

_TYPE_OBJECTS: dict[str, type] = {
    "bool": bool,
    "str": str,
    "int": int,
    "float": float,
    "list": list,
    "dict": dict,
}

_ALLOWED_COMPARE_OPS = (
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.In, ast.NotIn, ast.Is, ast.IsNot,
)

_ALLOWED_CONSTANT_TYPES = (str, int, float, bool, type(None))


class StateProxy:
    """Read-only attribute/subscript view over the run state dict.

    ``state.field`` and ``state['field']`` both resolve; nested dicts are
    wrapped so ``state.field.subfield`` works. A missing field raises
    ``KeyError`` — the evaluator converts it into ``OrchestrationError``
    naming the predicate.
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", data)

    def __getattr__(self, name: str) -> Any:
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        if name not in data:
            raise KeyError(name)
        return _wrap(data[name])

    def __getitem__(self, key: Any) -> Any:
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        return _wrap(data[key])

    def __contains__(self, key: Any) -> bool:
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        return key in data

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("state is read-only inside predicates")


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return StateProxy(value)
    return value


@dataclass(frozen=True)
class CompiledPredicate:
    """A validated, compiled ``when:`` expression."""

    source: str
    code: CodeType
    state_fields: tuple[str, ...]
    """Top-level ``state.<field>`` / ``state['field']`` names referenced."""

    def evaluate(self, state: dict[str, Any]) -> bool:
        """Evaluate against the run state. Missing field / type mishap →
        ``OrchestrationError`` carrying the predicate text."""
        try:
            result = eval(
                self.code,
                {"__builtins__": {}, **_ALLOWED_CALLS, **_TYPE_OBJECTS},
                {"state": StateProxy(state)},
            )
        except KeyError as exc:
            raise OrchestrationError(
                f"predicate {self.source!r} references state field "
                f"{exc.args[0]!r} which is missing from the current state",
                context={"predicate": self.source,
                         "missing_field": str(exc.args[0])},
                cause=exc,
            ) from exc
        except Exception as exc:
            raise OrchestrationError(
                f"predicate {self.source!r} raised "
                f"{type(exc).__name__}: {exc}",
                context={"predicate": self.source,
                         "cause_type": type(exc).__name__},
                cause=exc,
            ) from exc
        return bool(result)


def _forbid(node: ast.AST, source: str, why: str) -> CompileError:
    line = getattr(node, "lineno", 1)
    col = getattr(node, "col_offset", 0)
    return CompileError(
        f"predicate {source!r} contains a forbidden construct at "
        f"line {line}, column {col}: {why} (allowed: comparisons, "
        "and/or/not, state attribute access, subscripts, literals, and "
        f"calls to {', '.join(sorted(_ALLOWED_CALLS))})",
        context={
            "predicate": source,
            "line": line,
            "column": col,
            "construct": type(node).__name__,
            "why": why,
        },
    )


def _root_of_chain(node: ast.expr) -> ast.expr:
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    return current


class _Validator(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.state_fields: list[str] = []

    # --- structure ------------------------------------------------------

    def visit_Expression(self, node: ast.Expression) -> None:
        self.visit(node.body)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        for value in node.values:
            self.visit(value)

    def visit_UnaryOp(self, node: ast.UnaryOp) -> None:
        if not isinstance(node.op, (ast.Not, ast.USub)):
            raise _forbid(node, self.source,
                          f"unary operator {type(node.op).__name__}")
        self.visit(node.operand)

    def visit_Compare(self, node: ast.Compare) -> None:
        for op in node.ops:
            if not isinstance(op, _ALLOWED_COMPARE_OPS):
                raise _forbid(node, self.source,
                              f"comparison operator {type(op).__name__}")
        self.visit(node.left)
        for comparator in node.comparators:
            self.visit(comparator)

    # --- leaves ------------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, _ALLOWED_CONSTANT_TYPES):
            raise _forbid(node, self.source,
                          f"literal of type {type(node.value).__name__}")

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "state":
            return
        raise _forbid(
            node, self.source,
            f"name {node.id!r} (only 'state' and whitelisted calls exist)",
        )

    def visit_List(self, node: ast.List) -> None:
        for element in node.elts:
            self.visit(element)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        for element in node.elts:
            self.visit(element)

    def visit_Set(self, node: ast.Set) -> None:
        for element in node.elts:
            self.visit(element)

    # --- state access --------------------------------------------------------

    def visit_Attribute(self, node: ast.Attribute) -> None:
        root = _root_of_chain(node)
        if not (isinstance(root, ast.Name) and root.id == "state"):
            raise _forbid(
                node, self.source,
                "attribute access not rooted at 'state'",
            )
        # Dunder reflection is forbidden ANYWHERE in the chain (Phase 7
        # review finding 3): `state.__class__.__mro__` or
        # `state.__init__.__globals__` would compile and evaluate —
        # __class__ resolves on the proxy's TYPE, sidestepping __getattr__
        # — handing the predicate the interpreter's object graph.
        checked: ast.expr = node
        while isinstance(checked, (ast.Attribute, ast.Subscript)):
            if isinstance(checked, ast.Attribute) and (
                checked.attr.startswith("__") and checked.attr.endswith("__")
            ):
                raise _forbid(
                    checked, self.source,
                    f"dunder attribute {checked.attr!r} (reflection is "
                    "forbidden; predicates read state fields only)",
                )
            checked = checked.value
        # Record the TOP-LEVEL field: the attribute applied directly to
        # `state` in this chain.
        current: ast.expr = node
        top: str | None = None
        while isinstance(current, (ast.Attribute, ast.Subscript)):
            if isinstance(current, ast.Attribute) and isinstance(
                current.value, ast.Name
            ):
                top = current.attr
            current = current.value
        if top is not None:
            self.state_fields.append(top)
        # Walk any subscript slices inside the chain.
        current = node
        while isinstance(current, (ast.Attribute, ast.Subscript)):
            if isinstance(current, ast.Subscript):
                self.visit(current.slice)
            current = current.value

    def visit_Subscript(self, node: ast.Subscript) -> None:
        root = _root_of_chain(node)
        if not (isinstance(root, ast.Name) and root.id == "state"):
            raise _forbid(
                node, self.source, "subscript not rooted at 'state'"
            )
        if isinstance(node.value, ast.Name):
            # state['field'] — record the top-level field for coverage
            # validation when the key is a string literal.
            if isinstance(node.slice, ast.Constant) and isinstance(
                node.slice.value, str
            ):
                self.state_fields.append(node.slice.value)
        self.visit(node.slice)
        if not isinstance(node.value, ast.Name):
            self.visit(node.value)

    # --- calls ------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Name):
            raise _forbid(node, self.source, "call to a non-name expression")
        if node.func.id not in _ALLOWED_CALLS:
            raise _forbid(
                node, self.source,
                f"call to {node.func.id!r} (whitelist: "
                f"{', '.join(sorted(_ALLOWED_CALLS))})",
            )
        if node.keywords:
            raise _forbid(node, self.source, "keyword arguments")
        if node.func.id == "isinstance":
            if len(node.args) != 2:
                raise _forbid(node, self.source,
                              "isinstance takes exactly two arguments")
            type_arg = node.args[1]
            if not (
                isinstance(type_arg, ast.Name)
                and type_arg.id in _ALLOWED_ISINSTANCE_TYPES
            ):
                raise _forbid(
                    node, self.source,
                    "isinstance's second argument must be one of "
                    f"{', '.join(_ALLOWED_ISINSTANCE_TYPES)}",
                )
            self.visit(node.args[0])
            return
        if len(node.args) != 1:
            raise _forbid(
                node, self.source,
                f"{node.func.id} takes exactly one argument",
            )
        self.visit(node.args[0])

    # --- everything else is forbidden ------------------------------------------

    def generic_visit(self, node: ast.AST) -> None:
        raise _forbid(node, self.source, type(node).__name__)


def compile_predicate(
    source: str,
    *,
    state_fields: set[str] | None = None,
    where: str = "<flow>",
    pointer: str = "",
) -> CompiledPredicate:
    """Parse + AST-validate + compile a ``when:`` predicate.

    ``state_fields``, when given, validates every top-level
    ``state.<field>`` reference against the project's state schema —
    an unknown field is a ``CompileError`` (attribute access beyond
    allowed state fields, per the Phase 7 deliverable)."""
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise CompileError(
            f"predicate {source!r} is not a valid expression: {exc.msg} "
            f"(line {exc.lineno}, column {exc.offset})",
            context={"predicate": source, "file": where, "pointer": pointer},
            cause=exc,
        ) from exc
    validator = _Validator(source)
    try:
        validator.visit(tree)
    except CompileError as exc:
        exc.context.setdefault("file", where)
        exc.context.setdefault("pointer", pointer)
        raise
    if state_fields is not None:
        unknown = sorted(set(validator.state_fields) - state_fields)
        if unknown:
            raise CompileError(
                f"predicate {source!r} references unknown state field(s): "
                f"{', '.join(unknown)} (schema fields: "
                f"{', '.join(sorted(state_fields))})",
                context={
                    "predicate": source,
                    "unknown_fields": unknown,
                    "file": where,
                    "pointer": pointer,
                },
            )
    code = compile(tree, "<foundry-predicate>", "eval")
    return CompiledPredicate(
        source=source,
        code=code,
        state_fields=tuple(dict.fromkeys(validator.state_fields)),
    )


__all__ = [
    "CompiledPredicate",
    "StateProxy",
    "compile_predicate",
]
