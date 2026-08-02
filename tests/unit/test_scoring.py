from fraud_engine.domain.scoring import (
    Rule,
    Thresholds,
    apply_hard_actions,
    compute_score,
    decide,
    evaluate_rules,
    score_to_decision,
)

T = Thresholds(challenge_at=40, review_at=60, decline_at=80)

VELOCITY = Rule(
    code="VEL_CARD_1H",
    name="Card used 4+ times in an hour",
    condition={"feature": "velocity_card_1h", "op": "gte", "value": 4},
    weight=35,
)
NO_CVV = Rule(
    code="NO_CVV",
    name="CVV did not match",
    condition={"feature": "cvv_match", "op": "eq", "value": False},
    weight=25,
)
THREE_DS = Rule(
    code="THREE_DS_OK",
    name="3-D Secure authenticated",
    condition={"feature": "three_ds_status", "op": "eq", "value": "AUTHENTICATED"},
    weight=-30,
)
DENY = Rule(
    code="DENY_LIST",
    name="Entity is on the deny list",
    condition={"feature": "on_deny_list", "op": "eq", "value": True},
    weight=0,
    hard_action="DECLINE",
)
ALLOW = Rule(
    code="ALLOW_LIST",
    name="Entity is on the allow list",
    condition={"feature": "on_allow_list", "op": "eq", "value": True},
    weight=0,
    hard_action="APPROVE",
)
DISABLED = Rule(
    code="OFF",
    name="Disabled rule",
    condition={"feature": "amount_minor", "op": "gte", "value": 1},
    weight=100,
    is_enabled=False,
)


class TestEvaluateRules:
    def test_returns_only_matching_rules(self):
        hits = evaluate_rules([VELOCITY, NO_CVV], {"velocity_card_1h": 9, "cvv_match": True})
        assert [h.code for h in hits] == ["VEL_CARD_1H"]

    def test_skips_disabled_rules(self):
        hits = evaluate_rules([DISABLED], {"amount_minor": 999999})
        assert hits == []

    def test_evaluates_every_rule_even_after_a_hard_action(self):
        # Stopping early would make the recorded evidence depend on rule
        # ordering. The snapshot must show everything the engine saw.
        hits = evaluate_rules(
            [DENY, VELOCITY, NO_CVV],
            {"on_deny_list": True, "velocity_card_1h": 9, "cvv_match": False},
        )
        assert {h.code for h in hits} == {"DENY_LIST", "VEL_CARD_1H", "NO_CVV"}


class TestComputeScore:
    def test_sums_weights(self):
        hits = evaluate_rules([VELOCITY, NO_CVV], {"velocity_card_1h": 9, "cvv_match": False})
        assert compute_score(hits) == 60

    def test_negative_weights_pull_the_score_down(self):
        # Without this, a loyal customer's score only ever ratchets upward.
        hits = evaluate_rules(
            [VELOCITY, THREE_DS],
            {"velocity_card_1h": 5, "three_ds_status": "AUTHENTICATED"},
        )
        assert compute_score(hits) == 5

    def test_score_is_floored_at_zero(self):
        # A stack of good signals must not bank credit against a future bad
        # transaction.
        hits = evaluate_rules([THREE_DS], {"three_ds_status": "AUTHENTICATED"})
        assert compute_score(hits) == 0


class TestScoreToDecision:
    def test_bands(self):
        assert score_to_decision(0, T) == "APPROVE"
        assert score_to_decision(39, T) == "APPROVE"
        assert score_to_decision(40, T) == "CHALLENGE"
        assert score_to_decision(59, T) == "CHALLENGE"
        assert score_to_decision(60, T) == "REVIEW"
        assert score_to_decision(79, T) == "REVIEW"
        assert score_to_decision(80, T) == "DECLINE"
        assert score_to_decision(500, T) == "DECLINE"

    def test_bands_are_inclusive_at_the_lower_edge(self):
        # decline_at = 80 must decline at exactly 80: that is what the
        # analyst who typed 80 expects.
        assert score_to_decision(80, T) == "DECLINE"


class TestHardActions:
    def test_deny_list_overrides_a_clean_score(self):
        hits = evaluate_rules([DENY], {"on_deny_list": True})
        decision, applied = apply_hard_actions(score_to_decision(0, T), hits)
        assert decision == "DECLINE"
        assert applied == "DECLINE"

    def test_allow_list_does_not_rescue_a_declining_score(self):
        # An allow-listed card that is ALSO on the deny list stays declined.
        # The deny entry is newer information about the same card, and
        # failing safe is the correct default.
        hits = evaluate_rules(
            [ALLOW, DENY], {"on_allow_list": True, "on_deny_list": True}
        )
        decision, applied = apply_hard_actions(score_to_decision(0, T), hits)
        assert decision == "DECLINE"

    def test_allow_list_does_not_weaken_a_high_score(self):
        hits = evaluate_rules([ALLOW], {"on_allow_list": True})
        decision, applied = apply_hard_actions("DECLINE", hits)
        assert decision == "DECLINE"
        assert applied is None

    def test_no_hard_action_leaves_the_score_decision_alone(self):
        hits = evaluate_rules([VELOCITY], {"velocity_card_1h": 9})
        decision, applied = apply_hard_actions("CHALLENGE", hits)
        assert decision == "CHALLENGE"
        assert applied is None


class TestDecide:
    def test_a_clean_transaction_is_approved(self):
        result = decide(
            [VELOCITY, NO_CVV, THREE_DS, DENY],
            {
                "velocity_card_1h": 1,
                "cvv_match": True,
                "three_ds_status": "NOT_USED",
                "on_deny_list": False,
            },
            T,
        )
        assert result.decision == "APPROVE"
        assert result.score == 0
        assert result.hits == []

    def test_card_testing_pattern_is_declined(self):
        result = decide(
            [VELOCITY, NO_CVV, THREE_DS, DENY],
            {
                "velocity_card_1h": 11,
                "cvv_match": False,
                "three_ds_status": "NOT_USED",
                "on_deny_list": False,
            },
            T,
        )
        assert result.score == 60
        assert result.decision == "REVIEW"
        assert {h.code for h in result.hits} == {"VEL_CARD_1H", "NO_CVV"}

    def test_three_ds_rescues_an_otherwise_challenged_transaction(self):
        # Same velocity, but the issuer authenticated the cardholder and
        # carries the fraud liability. Declining here loses a good sale AND
        # the liability shift.
        risky = {"velocity_card_1h": 5, "cvv_match": True, "three_ds_status": "NOT_USED",
                 "on_deny_list": False}
        authed = {**risky, "three_ds_status": "AUTHENTICATED"}
        assert decide([VELOCITY, THREE_DS], risky, T).decision == "APPROVE"  # 35 < 40
        assert decide([VELOCITY, THREE_DS], authed, T).score == 5

    def test_deny_list_declines_regardless_of_everything_good(self):
        result = decide(
            [THREE_DS, DENY],
            {"three_ds_status": "AUTHENTICATED", "on_deny_list": True},
            T,
        )
        assert result.decision == "DECLINE"
        assert result.hard_action_applied == "DECLINE"
        assert result.score == 0  # floored, and irrelevant: the hard action won

    def test_the_result_serialises_for_the_frozen_snapshot(self):
        result = decide([VELOCITY], {"velocity_card_1h": 9}, T)
        payload = result.as_dict()
        assert payload["score"] == 35
        assert payload["triggered_rules"][0]["code"] == "VEL_CARD_1H"
        assert payload["triggered_rules"][0]["weight"] == 35
