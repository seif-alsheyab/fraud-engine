"""Integration test helper.

Each test runs inside a transaction that is ALWAYS rolled back, so nothing a
test writes survives it. No cleanup script, no leftover rows, and no test
that only passes when it runs first.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from fraud_engine.config import get_settings


@asynccontextmanager
async def rollback_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    """A connection whose work is always discarded."""
    settings = get_settings()
    conn = await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row, autocommit=False
    )
    try:
        yield conn
    finally:
        await conn.rollback()
        await conn.close()


async def seed_merchant(conn: psycopg.AsyncConnection, **over: Any) -> dict[str, Any]:
    suffix = uuid4().hex[:8]
    cur = await conn.execute(
        """
        INSERT INTO merchants (code, name, vertical, country, currency)
        VALUES (%s, %s, %s, %s, %s) RETURNING *
        """,
        (
            over.get("code", f"TEST-{suffix}"),
            over.get("name", "Test Merchant"),
            over.get("vertical", "DIGITAL"),
            over.get("country", "JO"),
            over.get("currency", "USD"),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def seed_ruleset(
    conn: psycopg.AsyncConnection, merchant_id: Any, **over: Any
) -> dict[str, Any]:
    cur = await conn.execute(
        """
        INSERT INTO rulesets (merchant_id, version, name, status,
                              challenge_at, review_at, decline_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            merchant_id,
            over.get("version", 1),
            over.get("name", "test ruleset"),
            over.get("status", "ACTIVE"),
            over.get("challenge_at", 40),
            over.get("review_at", 60),
            over.get("decline_at", 80),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def seed_rule(
    conn: psycopg.AsyncConnection, ruleset_id: Any, **over: Any
) -> dict[str, Any]:
    from psycopg.types.json import Jsonb

    cur = await conn.execute(
        """
        INSERT INTO rules (ruleset_id, code, name, condition, weight, hard_action, is_enabled)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            ruleset_id,
            over.get("code", f"R{uuid4().hex[:6].upper()}"),
            over.get("name", "test rule"),
            Jsonb(over.get("condition", {"feature": "amount_minor", "op": "gte", "value": 1})),
            over.get("weight", 10),
            over.get("hard_action"),
            over.get("is_enabled", True),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def seed_entity(
    conn: psycopg.AsyncConnection, entity_type: str = "CARD", **over: Any
) -> dict[str, Any]:
    cur = await conn.execute(
        """
        INSERT INTO entities (entity_type, value_hash, display_hint, first_seen_at, last_seen_at)
        VALUES (%s, %s, %s, %s, %s) RETURNING *
        """,
        (
            entity_type,
            over.get("value_hash", uuid4().hex),
            over.get("display_hint", "1234"),
            over.get("first_seen_at", datetime.now(UTC) - timedelta(days=30)),
            over.get("last_seen_at", datetime.now(UTC)),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def seed_transaction(
    conn: psycopg.AsyncConnection, merchant_id: Any, **over: Any
) -> dict[str, Any]:
    cur = await conn.execute(
        """
        INSERT INTO transactions (merchant_id, external_id, amount_minor, currency,
                                  card_entity_id, email_entity_id, device_entity_id,
                                  account_entity_id, occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
        """,
        (
            merchant_id,
            over.get("external_id", f"ext-{uuid4().hex[:10]}"),
            over.get("amount_minor", 25000),
            over.get("currency", "USD"),
            over.get("card_entity_id"),
            over.get("email_entity_id"),
            over.get("device_entity_id"),
            over.get("account_entity_id"),
            over.get("occurred_at", datetime.now(UTC)),
        ),
    )
    row = await cur.fetchone()
    assert row is not None
    return row
