"""The ieee-banded ruleset definition, checked without a database.

These tests exist to stop the measured weights being tidied. The numbers in
seed_ruleset.py came off a held-out fit; a future reader who rounds C12's 13
up to 15 for symmetry breaks the fit and nothing else would notice.
"""

import pytest

from fraud_engine.domain.conditions import evaluate_condition, validate_condition
from fraud_engine.lib.errors import RuleDefinitionError
from scripts.ieee.seed_ruleset import (
    CHALLENGE_AT,
    DECLINE_AT,
    REVIEW_AT,
    RULESET_VERSION,
    build_rules,
    validate_rules,
)

# Transcribed from the fit, independently of the module under test. If these
# two lists ever disagree, one of them was edited without a re-measurement.
EXPECTED_WEIGHTS = {
    "PRODUCT_NOT_W": 40,
    "LINK_DEVICE_ACCOUNTS": 20,
    "NEW_ACCOUNT_BURST": 35,
    "VEL_ACCOUNT_1H": 30,
    "VEL_ACCOUNT_24H": 15,
    "HIGH_AMOUNT": 25,
    "MED_AMOUNT": 15,
    "M4_ABSENT": -25,
    "VESTA_C4_GTE_1": 15, "VESTA_C4_GTE_2": 28, "VESTA_C4_GTE_4": 37,
    "VESTA_C8_GTE_1": 16, "VESTA_C8_GTE_3": 29, "VESTA_C8_GTE_8": 35,
    "VESTA_C10_GTE_1": 16, "VESTA_C10_GTE_3": 27, "VESTA_C10_GTE_10": 33,
    "VESTA_C12_GTE_1": 13, "VESTA_C12_GTE_2": 26, "VESTA_C12_GTE_4": 36,
    "VESTA_D3_LTE_8": 5,
    "VESTA_D5_LTE_9": 8, "VESTA_D5_LTE_32": 5,
}


class TestRulesetShape:
    def test_every_rule_carries_its_measured_weight(self):
        weights = {code: weight for code, _n, _c, weight in build_rules()}
        assert weights == EXPECTED_WEIGHTS

    def test_rule_codes_are_unique(self):
        codes = [code for code, *_ in build_rules()]
        assert len(codes) == len(set(codes)) == 23

    def test_the_protective_rule_is_the_only_negative_one(self):
        negative = [code for code, _n, _c, w in build_rules() if w < 0]
        # A scoring engine that can only add points can never be talked out of
        # a suspicion. M4_ABSENT is the one signal here that argues downwards.
        assert negative == ["M4_ABSENT"]

    def test_thresholds_are_ordered(self):
        assert CHALLENGE_AT < REVIEW_AT < DECLINE_AT
        assert (CHALLENGE_AT, REVIEW_AT, DECLINE_AT) == (147, 232, 357)
        assert RULESET_VERSION == 10


class TestDeclineIsReachable:
    def test_the_cumulative_bands_can_reach_decline_at(self):
        """The reason the bands are cumulative rather than exclusive.

        If each band excluded the one below it, the ceiling would be 319
        against a decline_at of 357 and the ruleset could never decline
        anything. That failure is silent -- every transaction simply comes
        back REVIEW at worst -- so it is asserted rather than assumed.
        """
        rules = build_rules()
        # HIGH_AMOUNT and MED_AMOUNT are bracketed apart and cannot both fire.
        ceiling = sum(w for _c, _n, _cond, w in rules if w > 0) - 15
        assert ceiling == 494
        assert ceiling >= DECLINE_AT

        exclusive_ceiling = 319
        assert exclusive_ceiling < DECLINE_AT

    def test_a_worst_case_transaction_actually_scores_past_decline_at(self):
        """Score a synthetic worst case through the real evaluator."""
        features = {
            "product_code": "C",
            "accounts_per_device_30d": 9,
            "account_age_days": 0,
            # NEW_ACCOUNT_BURST needs BOTH clauses. Without a seen_count the
            # rule does not fire and this stops being a worst case -- quietly,
            # since the total would still clear decline_at without it.
            "account_seen_count": 3,
            "velocity_account_1h": 5,
            "velocity_account_24h": 9,
            "amount_minor": 90000,
            "addr_match": "M2",
            "vesta_c4": 20, "vesta_c8": 20, "vesta_c10": 20, "vesta_c12": 20,
            "vesta_d3": 0, "vesta_d5": 0,
        }
        fired = {c for c, _n, cond, _w in build_rules() if evaluate_condition(cond, features)}
        score = sum(
            w for _c, _n, cond, w in build_rules() if evaluate_condition(cond, features)
        )
        # Named explicitly so a rule dropping out of the worst case is a
        # failure rather than a slightly smaller number nobody looks at.
        assert "NEW_ACCOUNT_BURST" in fired
        assert score >= DECLINE_AT


class TestAmountBracket:
    @pytest.mark.parametrize(
        "amount,med,high",
        [
            (29999, False, False),
            (30000, True, False),
            (49999, True, False),
            # The bracket closes exactly where HIGH_AMOUNT opens: no amount
            # fires both, and none falls between the two rules either.
            (50000, False, True),
            (90000, False, True),
        ],
    )
    def test_med_and_high_never_overlap_or_leave_a_gap(self, amount, med, high):
        conds = {code: cond for code, _n, cond, _w in build_rules()}
        features = {"amount_minor": amount}
        assert evaluate_condition(conds["MED_AMOUNT"], features) is med
        assert evaluate_condition(conds["HIGH_AMOUNT"], features) is high


class TestValidation:
    def test_a_rule_naming_an_unregistered_feature_is_rejected(self):
        bad = [("BAD", "bad rule", {"feature": "no_such_feature", "op": "gte", "value": 1}, 10)]
        with pytest.raises(RuleDefinitionError) as exc:
            validate_rules(bad, {"amount_minor"})
        assert "BAD" in str(exc.value)
        assert "no_such_feature" in str(exc.value)

    def test_a_duplicate_rule_code_is_rejected(self):
        cond = {"feature": "amount_minor", "op": "gte", "value": 1}
        dupe = [("SAME", "one", cond, 10), ("SAME", "two", cond, 20)]
        with pytest.raises(RuleDefinitionError, match="Duplicate rule code"):
            validate_rules(dupe, {"amount_minor"})

    def test_the_real_ruleset_passes_against_its_own_feature_set(self):
        rules = build_rules()
        referenced = set()
        for _c, _n, cond, _w in rules:
            for member in cond.get("all", [cond]):
                referenced.add(member["feature"])
        validate_rules(rules, referenced)

    def test_every_condition_is_structurally_valid(self):
        for code, _n, cond, _w in build_rules():
            validate_condition(cond, {"x"} | _features_of(cond), path=code)


def _features_of(cond):
    if "all" in cond:
        return {m["feature"] for m in cond["all"]}
    return {cond["feature"]}
