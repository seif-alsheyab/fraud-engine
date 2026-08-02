"""Transactions, decisions, labels, and review cases."""

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


async def insert_transaction(conn: AsyncConnection, t: dict[str, Any]) -> dict[str, Any]:
    cur = await conn.execute(
        """
        INSERT INTO transactions (
          merchant_id, external_id, amount_minor, currency,
          card_bin, card_last4, card_entity_id, email_entity_id,
          device_entity_id, ip_entity_id, account_entity_id,
          ip_address, ip_country, billing_country, shipping_country,
          avs_match, cvv_match, three_ds_status, is_card_present,
          channel, occurred_at
        ) VALUES (
          %(merchant_id)s, %(external_id)s, %(amount_minor)s, %(currency)s,
          %(card_bin)s, %(card_last4)s, %(card_entity_id)s, %(email_entity_id)s,
          %(device_entity_id)s, %(ip_entity_id)s, %(account_entity_id)s,
          %(ip_address)s, %(ip_country)s, %(billing_country)s, %(shipping_country)s,
          %(avs_match)s, %(cvv_match)s, %(three_ds_status)s, %(is_card_present)s,
          %(channel)s, %(occurred_at)s
        )
        RETURNING *
        """,
        t,
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def find_transaction_by_external_id(
    conn: AsyncConnection, merchant_id: UUID, external_id: str
) -> dict[str, Any] | None:
    """Idempotency lookup.

    A payment gateway that times out will retry. Without this, one payment
    becomes two transactions, two decisions, and doubled velocity counters
    for the honest customer who simply hit a slow network.
    """
    cur = await conn.execute(
        "SELECT * FROM transactions WHERE merchant_id = %s AND external_id = %s",
        (merchant_id, external_id),
    )
    return await cur.fetchone()


async def insert_decision(conn: AsyncConnection, d: dict[str, Any]) -> dict[str, Any]:
    """Store a decision with its frozen feature snapshot.

    Jsonb() wraps the dict so psycopg adapts it to a jsonb parameter.
    Passing a raw dict would be sent as a Python repr string and fail, and
    passing json.dumps() output would store it as text, losing every jsonb
    operator and index we might later want.
    """
    payload = dict(d)
    payload["features"] = Jsonb(d["features"])
    payload["triggered_rules"] = Jsonb(d.get("triggered_rules", []))
    cur = await conn.execute(
        """
        INSERT INTO decisions (
          transaction_id, ruleset_id, mode, decision, score,
          features, triggered_rules, latency_ms, exceeded_budget
        ) VALUES (
          %(transaction_id)s, %(ruleset_id)s, %(mode)s, %(decision)s, %(score)s,
          %(features)s, %(triggered_rules)s, %(latency_ms)s, %(exceeded_budget)s
        )
        RETURNING *
        """,
        payload,
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def find_decision(conn: AsyncConnection, decision_id: UUID) -> dict[str, Any] | None:
    cur = await conn.execute("SELECT * FROM decisions WHERE id = %s", (decision_id,))
    return await cur.fetchone()


async def find_live_decision_for_transaction(
    conn: AsyncConnection, transaction_id: UUID
) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT * FROM decisions WHERE transaction_id = %s AND mode = 'LIVE' "
        "ORDER BY created_at LIMIT 1",
        (transaction_id,),
    )
    return await cur.fetchone()


async def insert_label(conn: AsyncConnection, label: dict[str, Any]) -> dict[str, Any] | None:
    """Record the truth, arriving weeks after the decision.

    ON CONFLICT DO NOTHING on (transaction_id, source): the same chargeback
    file gets loaded twice more often than anyone admits, and a duplicated
    label would double-count fraud in every performance figure.
    """
    cur = await conn.execute(
        """
        INSERT INTO labels (transaction_id, label, source, reason_code,
                            amount_minor, labelled_at, days_to_label)
        VALUES (%(transaction_id)s, %(label)s, %(source)s, %(reason_code)s,
                %(amount_minor)s, %(labelled_at)s, %(days_to_label)s)
        ON CONFLICT (transaction_id, source) DO NOTHING
        RETURNING *
        """,
        label,
    )
    return await cur.fetchone()


async def open_review_case(
    conn: AsyncConnection, *, decision_id: UUID, sla_due_at: datetime
) -> dict[str, Any]:
    cur = await conn.execute(
        "INSERT INTO review_cases (decision_id, sla_due_at) VALUES (%s, %s) RETURNING *",
        (decision_id, sla_due_at),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def resolve_review_case(
    conn: AsyncConnection,
    *,
    case_id: UUID,
    disposition: str,
    analyst_note: str | None,
    assigned_to: str,
) -> dict[str, Any] | None:
    """Close a review case.

    The WHERE clause carries the expected status, so a case already resolved
    by another analyst returns None instead of being silently overwritten.
    """
    cur = await conn.execute(
        """
        UPDATE review_cases
           SET status = 'RESOLVED', disposition = %s, analyst_note = %s,
               assigned_to = %s, resolved_at = now()
         WHERE id = %s AND status IN ('OPEN','IN_PROGRESS')
        RETURNING *
        """,
        (disposition, analyst_note, assigned_to, case_id),
    )
    return await cur.fetchone()


async def list_open_review_cases(
    conn: AsyncConnection, *, limit: int = 50
) -> list[dict[str, Any]]:
    """The analyst queue, most urgent SLA first."""
    cur = await conn.execute(
        """
        SELECT rc.id, rc.status, rc.sla_due_at, rc.assigned_to,
               d.score, d.decision, t.amount_minor, t.currency, t.external_id
          FROM review_cases rc
          JOIN decisions d ON d.id = rc.decision_id
          JOIN transactions t ON t.id = d.transaction_id
         WHERE rc.status IN ('OPEN','IN_PROGRESS')
         ORDER BY rc.sla_due_at ASC
         LIMIT %s
        """,
        (limit,),
    )
    return await cur.fetchall()
