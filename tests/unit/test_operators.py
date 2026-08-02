import pytest

from fraud_engine.domain.operators import OPERATORS, op_gte, op_in
from fraud_engine.lib.errors import RuleDefinitionError


def test_all_six_operators_are_registered():
    assert set(OPERATORS) == {"eq", "ne", "gte", "lte", "in", "not_in"}


def test_gte_is_inclusive_at_the_boundary():
    # A threshold of 4 must fire at exactly 4. An analyst writing "4 or more"
    # means 4, and off-by-one here silently halves a rule's coverage.
    assert op_gte(4, 4) is True
    assert op_gte(3, 4) is False


def test_gte_refuses_a_boolean():
    # bool subclasses int in Python, so True would compare as 1 and quietly
    # satisfy a numeric threshold of 1.
    with pytest.raises(RuleDefinitionError):
        op_gte(True, 1)


def test_gte_refuses_a_string():
    with pytest.raises(RuleDefinitionError):
        op_gte("high", 4)


def test_in_requires_a_list():
    with pytest.raises(RuleDefinitionError):
        op_in("JO", "JO")


def test_in_matches_membership():
    assert op_in("NG", ["NG", "RU", "VN"]) is True
    assert op_in("JO", ["NG", "RU", "VN"]) is False
