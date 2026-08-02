from fraud_engine.domain.metrics import (
    base_fraud_rate,
    build_confusion_matrix,
    build_rule_performance,
)


def out(decision, label, amount=10000, rules=None):
    return {
        "decision": decision,
        "label": label,
        "amount_minor": amount,
        "triggered_rules": rules or [],
    }


class TestConfusionMatrix:
    def test_classifies_all_four_quadrants(self):
        m = build_confusion_matrix([
            out("DECLINE", "FRAUD"),        # caught it
            out("APPROVE", "LEGITIMATE"),   # correct approval
            out("DECLINE", "LEGITIMATE"),   # blocked a real customer
            out("APPROVE", "FRAUD"),        # fraud got through
        ])
        assert m.true_positive == 1
        assert m.true_negative == 1
        assert m.false_positive == 1
        assert m.false_negative == 1

    def test_review_counts_as_blocking(self):
        # A held order is not a completed sale. Counting REVIEW as an
        # approval would hide the friction it causes.
        m = build_confusion_matrix([out("REVIEW", "LEGITIMATE")])
        assert m.false_positive == 1

    def test_challenge_counts_as_allowing(self):
        # The payment proceeds; the customer just authenticates first.
        m = build_confusion_matrix([out("CHALLENGE", "LEGITIMATE")])
        assert m.true_negative == 1

    def test_unlabelled_rows_are_skipped_not_assumed_good(self):
        # Treating unknown as legitimate would silently inflate every
        # success metric on recent periods.
        m = build_confusion_matrix([out("APPROVE", None), out("APPROVE", "LEGITIMATE")])
        assert m.total == 1

    def test_blocking_everything_gives_perfect_recall_and_awful_precision(self):
        # The central lesson, as arithmetic: you caught all the fraud AND
        # destroyed the business.
        rows = [out("DECLINE", "FRAUD")] + [out("DECLINE", "LEGITIMATE") for _ in range(99)]
        m = build_confusion_matrix(rows)
        assert m.recall == 1.0
        assert m.precision == 0.01
        assert m.false_positive_rate == 1.0
        assert m.approval_rate == 0.0

    def test_approving_everything_gives_zero_recall_and_full_approval(self):
        rows = [out("APPROVE", "FRAUD")] + [out("APPROVE", "LEGITIMATE") for _ in range(99)]
        m = build_confusion_matrix(rows)
        assert m.recall == 0.0
        assert m.approval_rate == 1.0
        assert m.false_positive_rate == 0.0

    def test_precision_is_none_when_nothing_was_blocked(self):
        # "We were right about none of the things we blocked" is false when
        # you blocked nothing at all.
        m = build_confusion_matrix([out("APPROVE", "LEGITIMATE")])
        assert m.precision is None

    def test_recall_is_none_when_no_fraud_occurred(self):
        # 0% recall would send a team hunting a problem that never existed.
        m = build_confusion_matrix([out("APPROVE", "LEGITIMATE")])
        assert m.recall is None

    def test_f1_punishes_imbalance(self):
        # Perfect precision, near-zero recall: the arithmetic mean would say
        # 0.5, which flatters a system that catches almost nothing.
        rows = [out("DECLINE", "FRAUD")] + [out("APPROVE", "FRAUD") for _ in range(99)]
        m = build_confusion_matrix(rows)
        assert m.precision == 1.0
        assert m.recall == 0.01
        assert m.f1() is not None and m.f1() < 0.02

    def test_amounts_are_tracked_because_rates_hide_size(self):
        m = build_confusion_matrix([
            out("DECLINE", "LEGITIMATE", amount=500),      # blocked a $5 order
            out("APPROVE", "FRAUD", amount=500000),        # let $5,000 through
        ])
        assert m.fp_amount == 500
        assert m.fn_amount == 500000
        # By count this is a 50/50 split. By money it is a catastrophe:
        # 500000 of 500500 total value walked out of the door.
        # Expressed as the computation rather than a magic constant, so the
        # next reader does not have to reverse-engineer where it came from.
        expected_bps = round((500000 / (500000 + 500)) * 10000, 2)
        assert expected_bps == 9990.01
        assert m.fraud_rate_bps == expected_bps

    def test_an_empty_period_returns_none_everywhere_not_zero(self):
        m = build_confusion_matrix([])
        assert m.precision is None and m.recall is None
        assert m.fraud_rate_bps is None


class TestRulePerformance:
    def test_a_rule_is_judged_only_on_cases_where_it_fired(self):
        rows = [
            out("DECLINE", "FRAUD", rules=[{"code": "VEL", "name": "v", "weight": 35}]),
            out("DECLINE", "FRAUD", rules=[{"code": "VEL", "name": "v", "weight": 35}]),
            out("DECLINE", "LEGITIMATE", rules=[{"code": "VEL", "name": "v", "weight": 35}]),
            out("APPROVE", "LEGITIMATE"),
        ]
        perf = build_rule_performance(rows)[0]
        assert perf.fired_count == 3
        assert perf.precision is not None
        assert round(perf.precision, 3) == 0.667

    def test_lift_of_one_means_the_rule_carries_no_information(self):
        # Fires on fraud exactly as often as fraud occurs -> delete it.
        rows = [
            out("DECLINE", "FRAUD", rules=[{"code": "NOISE", "name": "n", "weight": 5}]),
            out("APPROVE", "LEGITIMATE", rules=[{"code": "NOISE", "name": "n", "weight": 5}]),
        ]
        rate = base_fraud_rate(rows)
        perf = build_rule_performance(rows)[0]
        assert rate == 0.5
        assert perf.lift(rate) == 1.0

    def test_a_high_lift_rule_is_concentrated_on_fraud(self):
        rows = [out("DECLINE", "FRAUD", rules=[{"code": "GOOD", "name": "g", "weight": 50}])]
        rows += [out("APPROVE", "LEGITIMATE") for _ in range(99)]
        rate = base_fraud_rate(rows)
        perf = build_rule_performance(rows)[0]
        assert perf.precision == 1.0
        assert perf.lift(rate) == 100.0

    def test_rules_are_ordered_by_how_often_they_fire(self):
        rows = [
            out("DECLINE", "FRAUD", rules=[{"code": "RARE", "name": "r", "weight": 1}]),
            out("DECLINE", "FRAUD", rules=[{"code": "LOUD", "name": "l", "weight": 1}]),
            out("DECLINE", "FRAUD", rules=[{"code": "LOUD", "name": "l", "weight": 1}]),
        ]
        assert [p.code for p in build_rule_performance(rows)] == ["LOUD", "RARE"]
