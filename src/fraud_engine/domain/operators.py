"""The complete set of operators a rule condition may use.

Six operators, implemented as plain functions. This is the entire vocabulary
of the rule language, and that narrowness is the point:

  * A rule cannot call a function, import a module, or touch the filesystem.
  * Every rule is inspectable, renderable in a UI, and diffable between
    versions.
  * An unknown operator fails when the rule is saved, not silently at 3am.

The alternative -- storing conditions as Python strings and calling eval() --
turns "let the risk team edit rules" into "let the risk team execute
arbitrary code on the payment server".
"""

from collections.abc import Callable
from typing import Any

from fraud_engine.lib.errors import RuleDefinitionError


def _as_number(value: Any, op: str) -> float:
    if isinstance(value, bool):
        # bool is a subclass of int in Python, so True would compare as 1
        # and quietly satisfy a numeric threshold. Refuse it explicitly.
        raise RuleDefinitionError(f"Operator '{op}' requires a number, got a boolean.")
    if not isinstance(value, (int, float)):
        raise RuleDefinitionError(f"Operator '{op}' requires a number, got {type(value).__name__}.")
    return float(value)


def op_eq(actual: Any, expected: Any) -> bool:
    return actual == expected


def op_ne(actual: Any, expected: Any) -> bool:
    return actual != expected


def op_gte(actual: Any, expected: Any) -> bool:
    return _as_number(actual, "gte") >= _as_number(expected, "gte")


def op_lte(actual: Any, expected: Any) -> bool:
    return _as_number(actual, "lte") <= _as_number(expected, "lte")


def op_in(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, (list, tuple)):
        raise RuleDefinitionError("Operator 'in' requires a list of values.")
    return actual in expected


def op_not_in(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, (list, tuple)):
        raise RuleDefinitionError("Operator 'not_in' requires a list of values.")
    return actual not in expected


OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": op_eq,
    "ne": op_ne,
    "gte": op_gte,
    "lte": op_lte,
    "in": op_in,
    "not_in": op_not_in,
}
