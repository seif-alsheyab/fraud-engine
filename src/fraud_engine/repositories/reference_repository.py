"""Reads of reference data: rulesets, rules, features, merchants.

Every function takes `conn` first. That can be a pooled connection in
production or a single connection inside a rolled-back transaction in tests.
The function neither knows nor cares which -- which is exactly what makes it
testable without a fixture teardown script.
"""

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection


async def find_merchant_by_code(conn: AsyncConnection, code: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        """
        SELECT id, code, name, vertical, country, currency, is_active
          FROM merchants WHERE code = %s
        """,
        (code,),
    )
    return await cur.fetchone()


async def find_merchant(conn: AsyncConnection, merchant_id: UUID) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT id, code, name, vertical, country, currency, is_active "
        "FROM merchants WHERE id = %s",
        (merchant_id,),
    )
    return await cur.fetchone()


async def find_ruleset_by_status(
    conn: AsyncConnection, merchant_id: UUID, status: str
) -> dict[str, Any] | None:
    """The ACTIVE (or SHADOW) ruleset for a merchant.

    A partial unique index guarantees at most one row per status, so this
    cannot silently return an arbitrary choice between two.
    """
    cur = await conn.execute(
        """
        SELECT id, merchant_id, version, name, status,
               challenge_at, review_at, decline_at
          FROM rulesets
         WHERE merchant_id = %s AND status = %s
        """,
        (merchant_id, status),
    )
    return await cur.fetchone()


async def find_ruleset(conn: AsyncConnection, ruleset_id: UUID) -> dict[str, Any] | None:
    cur = await conn.execute(
        """
        SELECT id, merchant_id, version, name, status,
               challenge_at, review_at, decline_at
          FROM rulesets WHERE id = %s
        """,
        (ruleset_id,),
    )
    return await cur.fetchone()


async def list_rules(conn: AsyncConnection, ruleset_id: UUID) -> list[dict[str, Any]]:
    """Enabled rules for a ruleset, in a stable order.

    Ordered by code so two evaluations of the same ruleset produce the
    triggered_rules list in the same sequence. Without ORDER BY, Postgres may
    return rows in any order, and two identical decisions would produce
    different-looking snapshots -- which makes diffing a backtest useless.
    """
    cur = await conn.execute(
        """
        SELECT id, code, name, description, condition, weight,
               hard_action, is_enabled
          FROM rules
         WHERE ruleset_id = %s AND is_enabled
         ORDER BY code
        """,
        (ruleset_id,),
    )
    return await cur.fetchall()


async def list_feature_codes(conn: AsyncConnection) -> set[str]:
    """The registry, used to validate a rule at write time."""
    cur = await conn.execute("SELECT code FROM feature_definitions")
    return {r["code"] for r in await cur.fetchall()}


async def list_feature_definitions(conn: AsyncConnection) -> list[dict[str, Any]]:
    cur = await conn.execute(
        "SELECT code, name, description, value_type, source "
        "FROM feature_definitions ORDER BY source, code"
    )
    return await cur.fetchall()


async def find_card_bin(conn: AsyncConnection, bin_value: str) -> dict[str, Any] | None:
    cur = await conn.execute(
        "SELECT bin, issuer_name, issuer_country, brand, card_type, is_prepaid "
        "FROM card_bins WHERE bin = %s",
        (bin_value,),
    )
    return await cur.fetchone()
