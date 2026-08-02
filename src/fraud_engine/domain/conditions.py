"""Rule condition evaluation.

A condition is JSON with one of three shapes:

    {"feature": "velocity_card_1h", "op": "gte", "value": 4}
    {"all": [ <condition>, <condition>, ... ]}     -- every one must match
    {"any": [ <condition>, <condition>, ... ]}     -- at least one must match

"all" and "any" nest, so arbitrarily complex logic is expressible without
ever leaving data. NOT is deliberately absent: "ne" and "not_in" cover the
real cases, and a negation operator over nested groups makes rules that
humans consistently misread.

Two entry points, and the split matters:

  validate_condition  runs when a rule is SAVED. Unknown feature, unknown
                      operator, malformed shape -> refuse to store it.
  evaluate_condition  runs on live traffic, on already-validated rules, and
                      is therefore free of validation overhead.

A MISSING feature at evaluation time returns False rather than raising. In
production, one broken feature must not take down every decision -- the rule
that depends on it simply does not fire, and the miss is recorded.
"""

from typing import Any

from fraud_engine.domain.operators import OPERATORS
from fraud_engine.lib.errors import RuleDefinitionError

_MISSING = object()


def validate_condition(condition: Any, known_features: set[str], path: str = "root") -> None:
    """Raise RuleDefinitionError if the condition is not well formed."""
    if not isinstance(condition, dict):
        raise RuleDefinitionError(f"Condition at {path} must be an object.")

    group_keys = [k for k in ("all", "any") if k in condition]

    if group_keys:
        if len(group_keys) > 1:
            raise RuleDefinitionError(f"Condition at {path} may not mix 'all' and 'any'.")
        key = group_keys[0]
        members = condition[key]
        if not isinstance(members, list) or not members:
            raise RuleDefinitionError(f"'{key}' at {path} must be a non-empty list.")
        for i, member in enumerate(members):
            validate_condition(member, known_features, f"{path}.{key}[{i}]")
        return

    for required in ("feature", "op", "value"):
        if required not in condition:
            raise RuleDefinitionError(f"Condition at {path} is missing '{required}'.")

    feature = condition["feature"]
    if feature not in known_features:
        raise RuleDefinitionError(
            f"Condition at {path} references unknown feature '{feature}'. "
            f"Register it in feature_definitions first."
        )

    op = condition["op"]
    if op not in OPERATORS:
        raise RuleDefinitionError(
            f"Condition at {path} uses unknown operator '{op}'. "
            f"Allowed: {', '.join(sorted(OPERATORS))}."
        )


def evaluate_condition(condition: dict[str, Any], features: dict[str, Any]) -> bool:
    """Evaluate a validated condition against a feature snapshot."""
    if "all" in condition:
        return all(evaluate_condition(m, features) for m in condition["all"])

    if "any" in condition:
        return any(evaluate_condition(m, features) for m in condition["any"])

    actual = features.get(condition["feature"], _MISSING)
    if actual is _MISSING or actual is None:
        # A feature that could not be computed does not match. Raising here
        # would let one failed lookup deny every transaction.
        return False

    return OPERATORS[condition["op"]](actual, condition["value"])


def referenced_features(condition: dict[str, Any]) -> set[str]:
    """Every feature a condition depends on.

    Used to compute only the features a ruleset actually needs, instead of
    running every velocity query on every transaction.
    """
    if "all" in condition:
        out: set[str] = set()
        for m in condition["all"]:
            out |= referenced_features(m)
        return out
    if "any" in condition:
        out = set()
        for m in condition["any"]:
            out |= referenced_features(m)
        return out
    return {condition["feature"]}
