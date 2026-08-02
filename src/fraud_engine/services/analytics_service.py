"""Performance reporting and backtesting."""

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection

from fraud_engine.domain.metrics import (
    BacktestComparison,
    base_fraud_rate,
    build_confusion_matrix,
    build_rule_performance,
)
from fraud_engine.domain.scoring import Rule, Thresholds, decide
from fraud_engine.lib.errors import NotFoundError
from fraud_engine.repositories import analytics_repository as ar
from fraud_engine.repositories import reference_repository as rr


async def performance_report(
    conn: AsyncConnection, *, merchant_id: UUID, since: datetime, before: datetime
) -> dict[str, Any]:
    """Confusion matrix, per-rule performance, and label coverage."""
    outcomes = await ar.labelled_outcomes(
        conn, merchant_id=merchant_id, since=since, before=before
    )
    matrix = build_confusion_matrix(outcomes)
    rate = base_fraud_rate(outcomes)
    rules = build_rule_performance(outcomes)
    coverage = await ar.label_coverage(
        conn, merchant_id=merchant_id, since=since, before=before
    )

    return {
        "period": {"since": since, "before": before},
        "coverage": coverage,
        "base_fraud_rate": rate,
        "matrix": matrix.as_dict(),
        "rules": [r.as_dict(rate) for r in rules],
    }


async def backtest_ruleset(
    conn: AsyncConnection,
    *,
    merchant_id: UUID,
    candidate_ruleset_id: UUID,
    since: datetime,
    before: datetime,
) -> dict[str, Any]:
    """Replay real history against a candidate ruleset.

    Uses the FROZEN feature snapshots, so the candidate answers exactly the
    questions the live engine faced -- not today's versions of them.

    Nothing is written: a backtest is a read-only thought experiment, and
    writing BACKTEST-mode decisions for every replay would bloat the table
    that production reads from.
    """
    candidate = await rr.find_ruleset(conn, candidate_ruleset_id)
    if candidate is None:
        raise NotFoundError(f"Ruleset {candidate_ruleset_id} not found.")

    rule_rows = await rr.list_rules(conn, candidate_ruleset_id)
    rules = [
        Rule(
            code=r["code"], name=r["name"], condition=r["condition"],
            weight=r["weight"], hard_action=r["hard_action"], is_enabled=r["is_enabled"],
        )
        for r in rule_rows
    ]
    thresholds = Thresholds(
        challenge_at=candidate["challenge_at"],
        review_at=candidate["review_at"],
        decline_at=candidate["decline_at"],
    )

    snapshots = await ar.replay_snapshots(
        conn, merchant_id=merchant_id, since=since, before=before
    )

    baseline_rows: list[dict[str, Any]] = []
    candidate_rows: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for s in snapshots:
        result = decide(rules, s["features"], thresholds)

        baseline_rows.append({
            "decision": s["baseline_decision"],
            "label": s["label"],
            "amount_minor": s["amount_minor"],
        })
        candidate_rows.append({
            "decision": result.decision,
            "label": s["label"],
            "amount_minor": s["amount_minor"],
        })

        if result.decision != s["baseline_decision"]:
            changed.append({
                "transaction_id": str(s["transaction_id"]),
                "amount_minor": s["amount_minor"],
                "label": s["label"],
                "was": s["baseline_decision"],
                "now": result.decision,
                "was_score": s["baseline_score"],
                "now_score": result.score,
            })

    comparison = BacktestComparison(
        baseline=build_confusion_matrix(baseline_rows),
        candidate=build_confusion_matrix(candidate_rows),
        changed=changed,
    )

    return {
        "candidate_ruleset": {
            "id": str(candidate["id"]),
            "version": candidate["version"],
            "name": candidate["name"],
            "status": candidate["status"],
        },
        "replayed_count": len(snapshots),
        **comparison.as_dict(),
    }
