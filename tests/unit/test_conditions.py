import pytest

from fraud_engine.domain.conditions import (
    evaluate_condition,
    referenced_features,
    validate_condition,
)
from fraud_engine.lib.errors import RuleDefinitionError

KNOWN = {"velocity_card_1h", "amount_minor", "three_ds_status", "on_deny_list", "cvv_match"}


class TestValidation:
    def test_accepts_a_simple_condition(self):
        validate_condition(
            {"feature": "velocity_card_1h", "op": "gte", "value": 4}, KNOWN
        )

    def test_rejects_an_unregistered_feature(self):
        # This is the whole point of the registry: a typo fails when the
        # rule is written, not by never matching in production.
        with pytest.raises(RuleDefinitionError, match="unknown feature"):
            validate_condition({"feature": "velocty_card_1h", "op": "gte", "value": 4}, KNOWN)

    def test_rejects_an_unknown_operator(self):
        with pytest.raises(RuleDefinitionError, match="unknown operator"):
            validate_condition({"feature": "amount_minor", "op": "roughly", "value": 4}, KNOWN)

    def test_rejects_a_missing_key(self):
        with pytest.raises(RuleDefinitionError, match="missing 'value'"):
            validate_condition({"feature": "amount_minor", "op": "gte"}, KNOWN)

    def test_rejects_an_empty_group(self):
        with pytest.raises(RuleDefinitionError, match="non-empty"):
            validate_condition({"all": []}, KNOWN)

    def test_rejects_mixing_all_and_any(self):
        with pytest.raises(RuleDefinitionError, match="may not mix"):
            validate_condition(
                {
                    "all": [{"feature": "amount_minor", "op": "gte", "value": 1}],
                    "any": [{"feature": "cvv_match", "op": "eq", "value": True}],
                },
                KNOWN,
            )

    def test_validates_nested_groups_and_reports_the_path(self):
        with pytest.raises(RuleDefinitionError, match=r"root\.all\[1\]\.any\[0\]"):
            validate_condition(
                {
                    "all": [
                        {"feature": "amount_minor", "op": "gte", "value": 1},
                        {"any": [{"feature": "nope", "op": "eq", "value": 1}]},
                    ]
                },
                KNOWN,
            )


class TestEvaluation:
    def test_simple_match(self):
        cond = {"feature": "velocity_card_1h", "op": "gte", "value": 4}
        assert evaluate_condition(cond, {"velocity_card_1h": 6}) is True
        assert evaluate_condition(cond, {"velocity_card_1h": 2}) is False

    def test_all_requires_every_member(self):
        cond = {
            "all": [
                {"feature": "amount_minor", "op": "gte", "value": 50000},
                {"feature": "cvv_match", "op": "eq", "value": False},
            ]
        }
        assert evaluate_condition(cond, {"amount_minor": 90000, "cvv_match": False}) is True
        assert evaluate_condition(cond, {"amount_minor": 90000, "cvv_match": True}) is False

    def test_any_requires_only_one(self):
        cond = {
            "any": [
                {"feature": "velocity_card_1h", "op": "gte", "value": 5},
                {"feature": "on_deny_list", "op": "eq", "value": True},
            ]
        }
        assert evaluate_condition(cond, {"velocity_card_1h": 1, "on_deny_list": True}) is True
        assert evaluate_condition(cond, {"velocity_card_1h": 1, "on_deny_list": False}) is False

    def test_a_missing_feature_does_not_match_and_does_not_raise(self):
        # In production one broken feature lookup must not deny every
        # transaction. The rule depending on it simply does not fire.
        cond = {"feature": "velocity_card_1h", "op": "gte", "value": 4}
        assert evaluate_condition(cond, {}) is False

    def test_a_none_feature_does_not_match(self):
        cond = {"feature": "velocity_card_1h", "op": "gte", "value": 4}
        assert evaluate_condition(cond, {"velocity_card_1h": None}) is False


def test_referenced_features_walks_nested_groups():
    cond = {
        "all": [
            {"feature": "amount_minor", "op": "gte", "value": 1},
            {
                "any": [
                    {"feature": "cvv_match", "op": "eq", "value": False},
                    {"feature": "three_ds_status", "op": "ne", "value": "AUTHENTICATED"},
                ]
            },
        ]
    }
    assert referenced_features(cond) == {"amount_minor", "cvv_match", "three_ds_status"}
