"""Velocity and shared-attribute queries.

These run on EVERY decision inside a 250ms budget, so the shape of each
query matters as much as its correctness.

Three rules followed throughout:

  1. Filter on the entity column FIRST, then time. The indexes are
     (entity_id, occurred_at DESC). Leading with time would force a scan of
     every entity's rows inside the window.

  2. Never wrap the timestamp column in a function. occurred_at >= %s uses
     the index; date_trunc('hour', occurred_at) = %s cannot. This is the
     same defect that silently under-counted chargebacks in the other repo.

  3. Compute several counters in ONE round trip where they share a window.
     Ten separate queries at 3ms each is 30ms of the budget spent on
     latency that buys nothing.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection


async def entity_velocity(
    conn: AsyncConnection,
    *,
    entity_column: str,
    entity_id: UUID,
    since: datetime,
    before: datetime,
) -> dict[str, Any]:
    """Count and total amount for one entity in a time window.

    entity_column is interpolated into the SQL rather than parameterised,
    because a column NAME cannot be a bind parameter in any SQL dialect. It
    is validated against a fixed allow-list first -- never taken from user
    input -- so this is not an injection path.

    `before` excludes the transaction being decided, so a card's first ever
    payment does not count itself as prior velocity.
    """
    allowed = {
        "card_entity_id",
        "email_entity_id",
        "device_entity_id",
        "ip_entity_id",
        "account_entity_id",
    }
    if entity_column not in allowed:
        raise ValueError(f"Unknown entity column: {entity_column}")

    cur = await conn.execute(
        f"""
        SELECT count(*)::int                            AS txn_count,
               COALESCE(sum(amount_minor), 0)::bigint   AS amount_total
          FROM transactions
         WHERE {entity_column} = %s
           AND occurred_at >= %s
           AND occurred_at <  %s
        """,  # noqa: S608 - entity_column is allow-listed above
        (entity_id, since, before),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def card_velocity_windows(
    conn: AsyncConnection,
    *,
    card_entity_id: UUID,
    now: datetime,
    one_hour_ago: datetime,
    one_day_ago: datetime,
) -> dict[str, Any]:
    """Both card windows in ONE query.

    FILTER (WHERE ...) computes several aggregates over a single scan of the
    same rows. Two separate queries would read the 24h range twice and pay
    two round trips for data already in memory.
    """
    cur = await conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE occurred_at >= %(hour)s)::int          AS count_1h,
          count(*) FILTER (WHERE occurred_at >= %(day)s)::int           AS count_24h,
          COALESCE(sum(amount_minor) FILTER (WHERE occurred_at >= %(day)s), 0)::bigint
                                                                        AS amount_24h
          FROM transactions
         WHERE card_entity_id = %(card)s
           AND occurred_at >= %(day)s
           AND occurred_at <  %(now)s
        """,
        {"card": card_entity_id, "hour": one_hour_ago, "day": one_day_ago, "now": now},
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def declines_for_card(
    conn: AsyncConnection, *, card_entity_id: UUID, since: datetime, before: datetime
) -> int:
    """Declines on this card in a window.

    Repeated declines followed by an approval is the textbook card-testing
    shape: the fraudster is probing until something works. Requires joining
    decisions, because a decline is an outcome, not a property of the
    transaction.
    """
    cur = await conn.execute(
        """
        SELECT count(*)::int AS n
          FROM transactions t
          JOIN decisions d ON d.transaction_id = t.id AND d.mode = 'LIVE'
         WHERE t.card_entity_id = %s
           AND t.occurred_at >= %s
           AND t.occurred_at <  %s
           AND d.decision = 'DECLINE'
        """,
        (card_entity_id, since, before),
    )
    row = await cur.fetchone()
    assert row is not None
    return row["n"]


async def shared_attribute_counts(
    conn: AsyncConnection,
    *,
    card_entity_id: UUID | None,
    account_entity_id: UUID | None,
    device_entity_id: UUID | None,
    since: datetime,
    before: datetime,
) -> dict[str, int]:
    """Cross-account linking: the signal a single transaction cannot reveal.

    Ten accounts with ten names and ten emails each look fine alone. All ten
    sharing one device fingerprint is one person wearing ten masks. This is
    a graph question hiding inside a payments question, and it is invisible
    unless you deliberately look ACROSS rows.

    Each count is DISTINCT and excludes NULL automatically, so a transaction
    missing a device fingerprint does not inflate anyone's link count.
    """
    result = {
        "accounts_per_card_30d": 0,
        "cards_per_account_30d": 0,
        "accounts_per_device_30d": 0,
        "emails_per_device_30d": 0,
    }

    if card_entity_id is not None:
        cur = await conn.execute(
            """
            SELECT count(DISTINCT account_entity_id)::int AS n
              FROM transactions
             WHERE card_entity_id = %s AND occurred_at >= %s AND occurred_at < %s
            """,
            (card_entity_id, since, before),
        )
        row = await cur.fetchone()
        result["accounts_per_card_30d"] = row["n"] if row else 0

    if account_entity_id is not None:
        cur = await conn.execute(
            """
            SELECT count(DISTINCT card_entity_id)::int AS n
              FROM transactions
             WHERE account_entity_id = %s AND occurred_at >= %s AND occurred_at < %s
            """,
            (account_entity_id, since, before),
        )
        row = await cur.fetchone()
        result["cards_per_account_30d"] = row["n"] if row else 0

    if device_entity_id is not None:
        cur = await conn.execute(
            """
            SELECT count(DISTINCT account_entity_id)::int AS accounts,
                   count(DISTINCT email_entity_id)::int   AS emails
              FROM transactions
             WHERE device_entity_id = %s AND occurred_at >= %s AND occurred_at < %s
            """,
            (device_entity_id, since, before),
        )
        row = await cur.fetchone()
        if row:
            result["accounts_per_device_30d"] = row["accounts"]
            result["emails_per_device_30d"] = row["emails"]

    return result
