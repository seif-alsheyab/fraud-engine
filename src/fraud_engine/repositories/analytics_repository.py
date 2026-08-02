"""Queries that join decisions to their eventual labels.

The join is the whole point: a decision alone tells you what you did, and a
label alone tells you what happened. Only together do they tell you whether
you were right.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection


async def labelled_outcomes(
    conn: AsyncConnection,
    *,
    merchant_id: UUID,
    since: datetime,
    before: datetime,
    mode: str = "LIVE",
) -> list[dict[str, Any]]:
    """Every decision in a window, with its label if one has arrived.

    LEFT JOIN, not INNER: unlabelled decisions must still appear so the
    caller can see how much of the period is still unknown. An inner join
    would silently report metrics on whatever fraction happens to be
    labelled, which flatters recent periods enormously.

    Labels are prioritised by source. A chargeback is stronger evidence than
    an assumption, so DISTINCT ON picks the most authoritative label when a
    transaction has several.
    """
    cur = await conn.execute(
        """
        WITH best_label AS (
          SELECT DISTINCT ON (transaction_id)
                 transaction_id, label, source, reason_code
            FROM labels
           ORDER BY transaction_id,
                    CASE source
                      WHEN 'CHARGEBACK'     THEN 1
                      WHEN 'ISSUER_REPORT'  THEN 2
                      WHEN 'MANUAL_REVIEW'  THEN 3
                      WHEN 'REFUND_REQUEST' THEN 4
                      ELSE 5
                    END
        )
        SELECT d.id AS decision_id, d.decision, d.score, d.triggered_rules,
               d.ruleset_id, d.features,
               t.id AS transaction_id, t.amount_minor, t.occurred_at,
               bl.label, bl.source AS label_source, bl.reason_code
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN best_label bl ON bl.transaction_id = t.id
         WHERE t.merchant_id = %s
           AND d.mode = %s
           AND t.occurred_at >= %s
           AND t.occurred_at <  %s
         ORDER BY t.occurred_at
        """,
        (merchant_id, mode, since, before),
    )
    return await cur.fetchall()


async def replay_snapshots(
    conn: AsyncConnection,
    *,
    merchant_id: UUID,
    since: datetime,
    before: datetime,
) -> list[dict[str, Any]]:
    """Frozen feature snapshots for backtesting.

    This is what makes backtesting honest. Recomputing features today would
    use TODAY's velocity counters -- a card quiet in March may be busy now,
    and the replay would answer a question that was never asked. The stored
    snapshot is the only record of what the engine actually saw at the time.
    """
    cur = await conn.execute(
        """
        WITH best_label AS (
          SELECT DISTINCT ON (transaction_id) transaction_id, label
            FROM labels
           ORDER BY transaction_id,
                    CASE source WHEN 'CHARGEBACK' THEN 1
                                WHEN 'ISSUER_REPORT' THEN 2
                                WHEN 'MANUAL_REVIEW' THEN 3 ELSE 4 END
        )
        SELECT d.id AS decision_id, d.features, d.decision AS baseline_decision,
               d.score AS baseline_score,
               t.id AS transaction_id, t.amount_minor, t.occurred_at,
               bl.label
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN best_label bl ON bl.transaction_id = t.id
         WHERE t.merchant_id = %s
           AND d.mode = 'LIVE'
           AND t.occurred_at >= %s
           AND t.occurred_at <  %s
         ORDER BY t.occurred_at
        """,
        (merchant_id, since, before),
    )
    return await cur.fetchall()


async def label_coverage(
    conn: AsyncConnection, *, merchant_id: UUID, since: datetime, before: datetime
) -> dict[str, Any]:
    """How much of a period has been labelled yet.

    Reported alongside every metric, because performance figures for last
    week are meaningless: chargebacks take 30-90 days to arrive, so recent
    periods always look fraud-free.
    """
    cur = await conn.execute(
        """
        SELECT count(*)::int                                        AS decisions,
               count(l.transaction_id)::int                         AS labelled,
               count(*) FILTER (WHERE l.label = 'FRAUD')::int        AS fraud_labels,
               COALESCE(round(avg(l.days_to_label)::numeric, 1), 0)  AS avg_days_to_label
          FROM decisions d
          JOIN transactions t ON t.id = d.transaction_id
          LEFT JOIN labels l ON l.transaction_id = t.id
         WHERE t.merchant_id = %s AND d.mode = 'LIVE'
           AND t.occurred_at >= %s AND t.occurred_at < %s
        """,
        (merchant_id, since, before),
    )
    row = await cur.fetchone()
    assert row is not None
    coverage = (
        round(row["labelled"] / row["decisions"], 4) if row["decisions"] else None
    )
    return {**row, "coverage": coverage}
