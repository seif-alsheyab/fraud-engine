"""Performance metrics.

Pure arithmetic on labelled outcomes. No database, no queries -- counts go
in, rates come out, so every edge case is testable in microseconds.

THE CONFUSION MATRIX, in payments terms:

                        truth: FRAUD          truth: LEGITIMATE
    blocked          true positive (TP)       false positive (FP)
    (decline/review)  caught it               blocked a real customer

    allowed          false negative (FN)      true negative (TN)
    (approve/          fraud got through       correct approval
     challenge)

Both errors cost money and they cost it DIFFERENTLY:
  FN -- lose the goods, the money, and pay a chargeback fee.
  FP -- lose one sale, and often the customer permanently.

A system with zero FN blocks everything and has zero revenue. A system with
zero FP approves everything and bleeds. The job is finding the cheapest
mixture, which is why every function below reports BOTH sides.
"""

from dataclasses import dataclass, field
from typing import Any

# Decisions that let money through. CHALLENGE is an approval: the payment
# proceeds, just with step-up authentication first.
ALLOWING_DECISIONS = {"APPROVE", "CHALLENGE"}
BLOCKING_DECISIONS = {"DECLINE", "REVIEW"}


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate, or None when there is nothing to divide by.

    None rather than 0.0, deliberately. "No fraud occurred" and "we caught
    0% of fraud" look identical as 0.0 and mean opposite things. A dashboard
    reporting 0% recall when there was simply no fraud to catch sends a team
    hunting a problem that does not exist.
    """
    if denominator == 0:
        return None
    return round(numerator / denominator, 6)


@dataclass
class ConfusionMatrix:
    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0

    # Value in minor units, because rates hide size. Ten blocked $5 orders
    # and one approved $5,000 fraud are not comparable as percentages.
    tp_amount: int = 0
    fp_amount: int = 0
    tn_amount: int = 0
    fn_amount: int = 0

    @property
    def total(self) -> int:
        return (
            self.true_positive + self.false_positive
            + self.true_negative + self.false_negative
        )

    @property
    def actual_fraud(self) -> int:
        return self.true_positive + self.false_negative

    @property
    def actual_legitimate(self) -> int:
        return self.false_positive + self.true_negative

    @property
    def blocked(self) -> int:
        return self.true_positive + self.false_positive

    @property
    def allowed(self) -> int:
        return self.true_negative + self.false_negative

    @property
    def precision(self) -> float | None:
        """Of everything blocked, how much was really fraud?

        Low precision means the team is punishing honest customers. This is
        the number a commercial director cares about.
        """
        return _rate(self.true_positive, self.blocked)

    @property
    def recall(self) -> float | None:
        """Of all the fraud, how much did we catch?

        Also called the detection rate. This is the number a risk manager
        cares about, and it pulls in the opposite direction to precision.
        """
        return _rate(self.true_positive, self.actual_fraud)

    @property
    def false_positive_rate(self) -> float | None:
        """Of all the GOOD customers, how many did we block?

        The most commercially expensive metric, and the one most often left
        off dashboards because it is unflattering.
        """
        return _rate(self.false_positive, self.actual_legitimate)

    @property
    def approval_rate(self) -> float | None:
        return _rate(self.allowed, self.total)

    @property
    def fraud_rate_bps(self) -> float | None:
        """Fraud that got through, in basis points of total value.

        Basis points because that is how card networks and acquirers express
        it, and because percentages of percentages get misread.
        """
        total_amount = self.tp_amount + self.fp_amount + self.tn_amount + self.fn_amount
        if total_amount == 0:
            return None
        return round((self.fn_amount / total_amount) * 10000, 2)

    def f1(self) -> float | None:
        """Harmonic mean of precision and recall.

        Harmonic, not arithmetic: it punishes imbalance. Precision 1.0 with
        recall 0.0 averages to 0.5 arithmetically, which would flatter a
        system that catches nothing. F1 gives it 0.0.
        """
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return round(2 * p * r / (p + r), 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "counts": {
                "true_positive": self.true_positive,
                "false_positive": self.false_positive,
                "true_negative": self.true_negative,
                "false_negative": self.false_negative,
                "total": self.total,
            },
            "amounts_minor": {
                "fraud_caught": self.tp_amount,
                "good_customers_blocked": self.fp_amount,
                "fraud_missed": self.fn_amount,
                "good_approved": self.tn_amount,
            },
            "rates": {
                "precision": self.precision,
                "recall": self.recall,
                "false_positive_rate": self.false_positive_rate,
                "approval_rate": self.approval_rate,
                "f1": self.f1(),
                "fraud_rate_bps": self.fraud_rate_bps,
            },
        }


def build_confusion_matrix(outcomes: list[dict[str, Any]]) -> ConfusionMatrix:
    """Fold labelled decisions into a matrix.

    Each outcome needs: decision, label, amount_minor.
    Rows with no label are SKIPPED, not counted as legitimate -- an unlabelled
    transaction is unknown, and treating unknown as good silently inflates
    every success metric.
    """
    m = ConfusionMatrix()
    for o in outcomes:
        label = o.get("label")
        if label is None:
            continue
        amount = o.get("amount_minor") or 0
        blocked = o["decision"] in BLOCKING_DECISIONS
        is_fraud = label == "FRAUD"

        if blocked and is_fraud:
            m.true_positive += 1
            m.tp_amount += amount
        elif blocked and not is_fraud:
            m.false_positive += 1
            m.fp_amount += amount
        elif not blocked and is_fraud:
            m.false_negative += 1
            m.fn_amount += amount
        else:
            m.true_negative += 1
            m.tn_amount += amount
    return m


@dataclass
class RulePerformance:
    """How one rule performs, judged only on the cases where it fired.

    A rule that fires on 10,000 transactions and is right 4% of the time is
    doing far more commercial damage than a rule that fires on 50 and is
    right 90% of the time -- even though both "catch fraud".
    """

    code: str
    name: str
    fired_count: int = 0
    fired_on_fraud: int = 0
    fired_on_legitimate: int = 0
    weight: int = 0

    @property
    def precision(self) -> float | None:
        labelled = self.fired_on_fraud + self.fired_on_legitimate
        return _rate(self.fired_on_fraud, labelled)

    def lift(self, base_fraud_rate: float | None) -> float | None:
        """How much better than guessing?

        Lift 1.0 means the rule is no better than the base rate: it fires on
        fraud exactly as often as fraud occurs, so it carries no information
        and should be deleted. Lift 5.0 means it is 5x more concentrated.
        """
        if base_fraud_rate is None or base_fraud_rate == 0:
            return None
        p = self.precision
        if p is None:
            return None
        return round(p / base_fraud_rate, 4)

    def as_dict(self, base_fraud_rate: float | None = None) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "weight": self.weight,
            "fired_count": self.fired_count,
            "fired_on_fraud": self.fired_on_fraud,
            "fired_on_legitimate": self.fired_on_legitimate,
            "precision": self.precision,
            "lift": self.lift(base_fraud_rate),
        }


def build_rule_performance(outcomes: list[dict[str, Any]]) -> list[RulePerformance]:
    """Per-rule statistics from decisions carrying their triggered_rules."""
    by_code: dict[str, RulePerformance] = {}

    for o in outcomes:
        for hit in o.get("triggered_rules") or []:
            code = hit["code"]
            perf = by_code.get(code)
            if perf is None:
                perf = RulePerformance(
                    code=code, name=hit.get("name", code), weight=hit.get("weight", 0)
                )
                by_code[code] = perf
            perf.fired_count += 1
            label = o.get("label")
            if label == "FRAUD":
                perf.fired_on_fraud += 1
            elif label is not None:
                perf.fired_on_legitimate += 1

    return sorted(by_code.values(), key=lambda p: p.fired_count, reverse=True)


def base_fraud_rate(outcomes: list[dict[str, Any]]) -> float | None:
    """Fraction of LABELLED transactions that were fraud."""
    labelled = [o for o in outcomes if o.get("label") is not None]
    if not labelled:
        return None
    fraud = sum(1 for o in labelled if o["label"] == "FRAUD")
    return round(fraud / len(labelled), 6)


@dataclass
class BacktestComparison:
    """Live ruleset versus a candidate, on identical history.

    The only honest way to answer "should we ship this rule change?" -- the
    same transactions, the same frozen features, two different rulesets.
    """

    baseline: ConfusionMatrix
    candidate: ConfusionMatrix
    changed: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        b, c = self.baseline, self.candidate

        def delta(x: float | None, y: float | None) -> float | None:
            if x is None or y is None:
                return None
            return round(y - x, 6)

        return {
            "baseline": b.as_dict(),
            "candidate": c.as_dict(),
            "delta": {
                "precision": delta(b.precision, c.precision),
                "recall": delta(b.recall, c.recall),
                "false_positive_rate": delta(b.false_positive_rate, c.false_positive_rate),
                "approval_rate": delta(b.approval_rate, c.approval_rate),
                # The two numbers a decision actually turns on:
                "extra_fraud_caught_minor": c.tp_amount - b.tp_amount,
                "extra_good_customers_blocked": c.false_positive - b.false_positive,
            },
            "changed_decision_count": len(self.changed),
            "changed_sample": self.changed[:20],
        }
