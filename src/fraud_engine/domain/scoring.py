"""Rule evaluation, scoring, and the decision policy.

Pure functions throughout: rules and features arrive as arguments, never
fetched. Same discipline as the chargeback state machine -- it is why these
tests run in milliseconds with no database, and why the identical function
is exercised against fake data in unit tests and real rows in integration
tests.
"""

from dataclasses import dataclass, field
from typing import Any

from fraud_engine.domain.conditions import evaluate_condition

# Rank used when a hard action and the score disagree. The most restrictive
# outcome wins: a deny-list hit must never be talked out of by good signals.
_SEVERITY = {"APPROVE": 0, "CHALLENGE": 1, "REVIEW": 2, "DECLINE": 3}


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    condition: dict[str, Any]
    weight: int
    hard_action: str | None = None
    is_enabled: bool = True


@dataclass(frozen=True)
class Thresholds:
    challenge_at: int = 40
    review_at: int = 60
    decline_at: int = 80


@dataclass
class RuleHit:
    code: str
    name: str
    weight: int
    hard_action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "weight": self.weight,
            "hard_action": self.hard_action,
        }


@dataclass
class Evaluation:
    score: int
    decision: str
    hits: list[RuleHit] = field(default_factory=list)
    hard_action_applied: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "decision": self.decision,
            "hard_action_applied": self.hard_action_applied,
            "triggered_rules": [h.as_dict() for h in self.hits],
        }


def evaluate_rules(rules: list[Rule], features: dict[str, Any]) -> list[RuleHit]:
    """Every enabled rule whose condition matches.

    ALL rules are evaluated, not just up to the first hard action. Stopping
    early would make the record of what fired depend on rule ordering, and
    the whole point of the frozen snapshot is being able to answer "what did
    the engine see?" completely.
    """
    return [
        RuleHit(code=r.code, name=r.name, weight=r.weight, hard_action=r.hard_action)
        for r in rules
        if r.is_enabled and evaluate_condition(r.condition, features)
    ]


def compute_score(hits: list[RuleHit]) -> int:
    """Sum the weights, floored at zero.

    Negative weights are intentional: 3DS authentication and a long good
    history should pull the score DOWN. Without them the engine can only
    ever become more suspicious, and a loyal customer's score ratchets
    upward forever.

    Flooring at zero keeps the number interpretable as risk. A score of -40
    is not "safer than safe", it is just noise, and it would let a stack of
    good signals bank credit against a future genuinely bad transaction.
    """
    return max(0, sum(h.weight for h in hits))


def score_to_decision(score: int, t: Thresholds) -> str:
    """Map a score onto a decision band.

    Bands are inclusive at the lower edge. A ruleset with decline_at = 80
    declines at exactly 80, which is what a risk analyst setting that number
    expects.
    """
    if score >= t.decline_at:
        return "DECLINE"
    if score >= t.review_at:
        return "REVIEW"
    if score >= t.challenge_at:
        return "CHALLENGE"
    return "APPROVE"


def apply_hard_actions(score_decision: str, hits: list[RuleHit]) -> tuple[str, str | None]:
    """Reconcile hard actions with the score-derived decision.

    The most restrictive outcome wins. Two consequences worth stating:

      * A DECLINE hard action (deny list) cannot be overridden by any number
        of good signals. That is the entire purpose of a deny list.
      * An APPROVE hard action (allow list) does NOT override a DECLINE from
        elsewhere. An allow-listed card that is now on a deny list is still
        declined -- because the deny entry is newer information about the
        same card, and failing safe is the right default.
    """
    hard = [h.hard_action for h in hits if h.hard_action]
    if not hard:
        return score_decision, None

    strongest = max(hard, key=lambda a: _SEVERITY[a])
    if _SEVERITY[strongest] >= _SEVERITY[score_decision]:
        return strongest, strongest
    return score_decision, None


def decide(rules: list[Rule], features: dict[str, Any], thresholds: Thresholds) -> Evaluation:
    """The whole decision, in one pure function."""
    hits = evaluate_rules(rules, features)
    score = compute_score(hits)
    from_score = score_to_decision(score, thresholds)
    decision, hard_applied = apply_hard_actions(from_score, hits)
    return Evaluation(
        score=score, decision=decision, hits=hits, hard_action_applied=hard_applied
    )
