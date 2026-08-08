"""transaction_features(): the part that costs no query.

Every assertion here is about the difference between "absent" and "false".
CLAUDE.md §5.8 states it for rates; it applies just as hard to categoricals.
A rule reading `card_type eq "credit"` must not fire because nothing was
supplied and something helpful guessed.
"""

import pytest

from fraud_engine.services.feature_service import (
    COMPUTABLE_FEATURES,
    ENGINE_COMPUTED_FEATURES,
    SUPPLIED_ONLY_FEATURES,
    transaction_features,
)

BASE = {"amount_minor": 25000}


def _features(**over):
    return transaction_features({**BASE, **over}, None)


class TestProductCode:
    def test_it_is_passed_through(self):
        assert _features(product_code="C")["product_code"] == "C"

    def test_an_absent_product_code_is_none_not_a_default(self):
        # A `ne "W"` rule must NOT fire on a transaction that supplied
        # nothing: evaluate_condition treats None as no-match, which is the
        # correct behaviour only if the value really is None.
        assert _features()["product_code"] is None

    def test_a_blank_string_is_treated_as_absent(self):
        assert _features(product_code="   ")["product_code"] is None


class TestCardType:
    def test_it_is_passed_through(self):
        assert _features(card_type="debit")["card_type"] == "debit"

    def test_an_absent_card_type_is_none(self):
        assert _features()["card_type"] is None


class TestAddrMatch:
    def test_a_supplied_value_is_kept(self):
        assert _features(addr_match="M2")["addr_match"] == "M2"

    def test_absence_becomes_the_explicit_absent_category(self):
        """M4's absence is a measured category, not a missing value.

        The M4_ABSENT rule tests `addr_match eq "(absent)"`. Left as None it
        would never match, and the protective rule holding down the score on
        ordinary traffic would silently stop working.
        """
        assert _features()["addr_match"] == "(absent)"
        assert _features(addr_match=None)["addr_match"] == "(absent)"
        assert _features(addr_match="")["addr_match"] == "(absent)"


class TestDistFromBilling:
    def test_it_is_passed_through_as_a_float(self):
        assert _features(dist_from_billing=19)["dist_from_billing"] == 19.0

    def test_zero_survives(self):
        """0 is a real distance -- the transaction is at the billing address.

        `or None` here would report the least suspicious possible value as
        unknown, which is the exact inversion of the truth.
        """
        assert _features(dist_from_billing=0)["dist_from_billing"] == 0.0

    def test_an_absent_distance_is_none(self):
        assert _features()["dist_from_billing"] is None


class TestHasIdentityData:
    def test_true_and_false_are_both_preserved(self):
        assert _features(has_identity_data=True)["has_identity_data"] is True
        assert _features(has_identity_data=False)["has_identity_data"] is False

    def test_an_absent_flag_is_none_not_false(self):
        """Tri-state, deliberately.

        False means the join ran and found nothing. None means nobody said.
        Collapsing them would make every caller that omits the field look
        like a transaction with no identity data at all -- and in the IEEE
        data that is 75% of rows, so a NO_IDENTITY rule would fire on
        essentially everything.
        """
        assert _features()["has_identity_data"] is None


class TestTheDeclaredSets:
    def test_supplied_only_is_exactly_the_vesta_family(self):
        expected = {
            "vesta_c4", "vesta_c8", "vesta_c10", "vesta_c12", "vesta_d3", "vesta_d5",
        }
        assert set(SUPPLIED_ONLY_FEATURES) == expected

    def test_no_vesta_feature_claims_to_be_engine_computed(self):
        """The distinction has to survive in the code, not just in prose.

        If a vesta_ feature ever appeared in ENGINE_COMPUTED_FEATURES the
        reachability test would pass while nothing computed it, restoring
        exactly the silent failure this whole change exists to remove.
        """
        assert not any(f.startswith("vesta_") for f in ENGINE_COMPUTED_FEATURES)

    @pytest.mark.parametrize(
        "feature",
        ["product_code", "card_type", "addr_match", "dist_from_billing", "has_identity_data"],
    )
    def test_the_new_transaction_features_are_produced_with_no_input_at_all(self, feature):
        """Present as a key even when nothing was supplied.

        A key that is absent and a key set to None behave identically in the
        evaluator, but only the second is visible in the frozen snapshot as
        evidence that the engine looked and found nothing.
        """
        assert feature in _features()
        assert feature in COMPUTABLE_FEATURES
