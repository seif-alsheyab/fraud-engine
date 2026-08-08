"""T7's arithmetic, checked against hand-computable cases.

No database. Every expected value below is derived by hand in the test rather
than copied from a run of the code -- a test that asserts whatever the
implementation happened to produce documents nothing (CLAUDE.md §8: "Guessing
an expected value in an assertion").

Ties are the recurring theme. The score is a sum of integer rule weights, so
590,540 decisions land on 406 distinct values; a tie-blind AUC would report a
materially higher figure for the same engine.
"""

import pytest

from scripts.ieee.measure import (
    pr_auc,
    roc_auc_by_rank,
    roc_auc_by_trapezoid,
    threshold_sweep,
    unique_contributions,
)


class TestRocAuc:
    def test_perfect_separation_is_one(self):
        points = [(10, True), (9, True), (2, False), (1, False)]
        assert roc_auc_by_rank(points) == 1.0

    def test_perfectly_inverted_is_zero(self):
        points = [(1, True), (2, True), (9, False), (10, False)]
        assert roc_auc_by_rank(points) == 0.0

    def test_every_score_tied_is_one_half(self):
        """The case that separates a correct implementation from a wrong one.

        If every transaction scores the same, the ranking carries no
        information and the answer is 0.5. An implementation that breaks ties
        by list order returns 1.0 or 0.0 here and looks superb.
        """
        points = [(5, True), (5, False), (5, True), (5, False)]
        assert roc_auc_by_rank(points) == 0.5

    def test_a_hand_computed_case(self):
        # Positives score {3, 1}; negatives score {2, 0}.
        # Pairs: 3>2 win, 3>0 win, 1<2 loss, 1>0 win  ->  3 of 4.
        points = [(3, True), (1, True), (2, False), (0, False)]
        assert roc_auc_by_rank(points) == 0.75

    def test_a_half_tied_pair_counts_as_half_a_win(self):
        # Positive 2 vs negatives {2, 0}: one tie (0.5) + one win (1.0) = 1.5/2
        points = [(2, True), (2, False), (0, False)]
        assert roc_auc_by_rank(points) == 0.75

    @pytest.mark.parametrize(
        "points",
        [
            [(3, True), (1, True), (2, False), (0, False)],
            [(5, True), (5, False), (5, True), (5, False)],
            [(2, True), (2, False), (0, False)],
            [(9, True), (4, False), (4, True), (1, False), (7, True)],
        ],
    )
    def test_both_methods_agree(self, points):
        """Rank statistic and curve integration must give the same number.

        They are computed by different code for exactly this reason: agreement
        is evidence the tie handling is right in both.
        """
        assert roc_auc_by_rank(points) == pytest.approx(roc_auc_by_trapezoid(points))

    def test_one_class_only_is_none_not_zero(self):
        """None, not 0.0 (CLAUDE.md §5.8).

        "No fraud occurred" and "the ranking was useless" are different facts,
        and 0.0 would report the second when the first is true.
        """
        assert roc_auc_by_rank([(1, True), (2, True)]) is None
        assert roc_auc_by_rank([(1, False), (2, False)]) is None


class TestPrAuc:
    def test_perfect_separation_is_one(self):
        assert pr_auc([(10, True), (9, True), (2, False), (1, False)]) == 1.0

    def test_a_single_tied_pair_is_the_base_rate(self):
        # One group, precision 0.5, recall steps 0 -> 1: area = 0.5
        assert pr_auc([(5, True), (5, False)]) == 0.5

    def test_a_hand_computed_case(self):
        # Scores desc: 3(pos), 2(neg), 1(pos), 0(neg). n_pos = 2.
        #   after 3: tp=1 fp=0 -> P=1.000, R=0.5 -> (0.5-0.0)*1.000  = 0.5
        #   after 2: tp=1 fp=1 -> P=0.500, R=0.5 -> no recall change = 0.0
        #   after 1: tp=2 fp=1 -> P=0.667, R=1.0 -> (1.0-0.5)*0.667  = 0.3333
        #   after 0: tp=2 fp=2 -> P=0.500, R=1.0 -> no recall change = 0.0
        points = [(3, True), (2, False), (1, True), (0, False)]
        assert pr_auc(points) == pytest.approx(0.5 + (2 / 3) * 0.5)

    def test_one_class_only_is_none(self):
        assert pr_auc([(1, True), (2, True)]) is None
        assert pr_auc([(1, False), (2, False)]) is None


class TestThresholdSweep:
    def test_it_reports_every_distinct_score_as_an_operating_point(self):
        points = [(3, True), (2, False), (1, True), (0, False)]
        rows = threshold_sweep(points)
        assert [r["threshold"] for r in rows] == [0, 1, 2, 3]

    def test_blocking_is_inclusive_of_the_threshold(self):
        """block iff score >= t, matching the engine's own banding."""
        rows = {r["threshold"]: r for r in threshold_sweep(
            [(3, True), (2, False), (1, True), (0, False)]
        )}
        assert rows[3]["blocked"] == 1
        assert rows[2]["blocked"] == 2
        assert rows[0]["blocked"] == 4

    def test_the_cells_always_sum_to_the_population(self):
        points = [(9, True), (4, False), (4, True), (1, False), (7, True)]
        for row in threshold_sweep(points):
            assert row["tp"] + row["fp"] + row["fn"] + row["tn"] == len(points)

    def test_recall_is_monotonic_as_the_threshold_falls(self):
        points = [(9, True), (4, False), (4, True), (1, False), (7, True)]
        rows = threshold_sweep(points)  # ascending threshold
        recalls = [r["recall"] for r in rows]
        assert recalls == sorted(recalls, reverse=True)

    def test_an_empty_population_is_an_empty_sweep(self):
        assert threshold_sweep([]) == []


class TestUniqueContributions:
    """Fraud a rule caught that no other rule would have caught alone.

    The diagnostic that condemned LINK_DEVICE_ACCOUNTS in the superseded
    ruleset: good precision, zero unique contribution, therefore redundant.
    """

    WEIGHTS = {"A": 40, "B": 20, "C": 5}

    def _outcome(self, score, label, codes):
        return {"score": score, "label": label,
                "triggered_rules": [{"code": c} for c in codes]}

    def test_a_rule_that_alone_pushes_past_the_threshold_gets_credit(self):
        # 50 >= 40; removing A leaves 10, below -> A is load-bearing.
        outcomes = [self._outcome(50, "FRAUD", ["A", "B"])]
        assert unique_contributions(outcomes, self.WEIGHTS, 40)["A"] == 1

    def test_a_rule_the_score_does_not_depend_on_gets_none(self):
        # 65 - 5 = 60, still >= 40, so C changed nothing.
        outcomes = [self._outcome(65, "FRAUD", ["A", "B", "C"])]
        assert unique_contributions(outcomes, self.WEIGHTS, 40)["C"] == 0

    def test_two_rules_can_both_be_load_bearing_on_one_transaction(self):
        # 60 with A(40) and B(20): removing either drops below 60.
        outcomes = [self._outcome(60, "FRAUD", ["A", "B"])]
        result = unique_contributions(outcomes, self.WEIGHTS, 60)
        assert result["A"] == 1 and result["B"] == 1

    def test_legitimate_transactions_are_not_counted(self):
        outcomes = [self._outcome(50, "LEGITIMATE", ["A"])]
        assert unique_contributions(outcomes, self.WEIGHTS, 40)["A"] == 0

    def test_an_unblocked_transaction_is_not_counted(self):
        """No credit for fraud that was let through."""
        outcomes = [self._outcome(30, "FRAUD", ["B", "C"])]
        assert unique_contributions(outcomes, self.WEIGHTS, 40)["B"] == 0

    def test_every_known_rule_appears_even_at_zero(self):
        """A rule that never fired must still be listed.

        An absent row reads as "not measured"; an explicit 0 reads as
        "measured, contributed nothing" -- and only the second tells a reader
        the rule is a candidate for deletion.
        """
        result = unique_contributions([], self.WEIGHTS, 40)
        assert result == {"A": 0, "B": 0, "C": 0}
