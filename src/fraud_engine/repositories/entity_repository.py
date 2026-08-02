"""Entity storage and lookup.

An entity row is created the first time an identifier is seen and updated on
every subsequent sighting. Both happen in one statement, because the
alternative -- SELECT then INSERT -- leaves a window where two concurrent
transactions both find nothing and both insert, and the unique constraint
turns one of them into an error on a perfectly valid payment.
"""

from typing import Any
from uuid import UUID

from psycopg import AsyncConnection


async def upsert_entity(
    conn: AsyncConnection,
    entity_type: str,
    value_hash: str,
    display_hint: str | None,
    seen_at: Any,
) -> dict[str, Any]:
    """Create or touch an entity, returning its current state.

    IMPORTANT: first_seen_at is deliberately NOT updated on conflict. Entity
    age is a signal -- a card first seen four seconds ago is very different
    from one first seen two years ago -- and overwriting it on every sighting
    would make every entity permanently brand new.

    seen_count is incremented here rather than counted from transactions,
    because counting on every decision would not fit the latency budget.
    """
    cur = await conn.execute(
        """
        INSERT INTO entities (entity_type, value_hash, display_hint,
                              first_seen_at, last_seen_at, seen_count)
        VALUES (%s, %s, %s, %s, %s, 1)
        ON CONFLICT (entity_type, value_hash) DO UPDATE
           SET last_seen_at = GREATEST(entities.last_seen_at, EXCLUDED.last_seen_at),
               seen_count   = entities.seen_count + 1,
               display_hint = COALESCE(entities.display_hint, EXCLUDED.display_hint)
        RETURNING id, entity_type, value_hash, display_hint,
                  first_seen_at, last_seen_at, seen_count
        """,
        (entity_type, value_hash, display_hint, seen_at, seen_at),
    )
    row = await cur.fetchone()
    assert row is not None
    return row


async def find_entity(
    conn: AsyncConnection, entity_type: str, value_hash: str
) -> dict[str, Any] | None:
    cur = await conn.execute(
        """
        SELECT id, entity_type, value_hash, display_hint,
               first_seen_at, last_seen_at, seen_count
          FROM entities WHERE entity_type = %s AND value_hash = %s
        """,
        (entity_type, value_hash),
    )
    return await cur.fetchone()


async def list_active_list_entries(
    conn: AsyncConnection, entity_ids: list[UUID], merchant_id: UUID
) -> list[dict[str, Any]]:
    """Allow / deny / watch entries covering any of these entities.

    Two filters that matter:

      * expires_at IS NULL OR expires_at > now()  -- an expired entry must
        stop applying automatically. A permanent block on an IP address is a
        mistake: addresses get reassigned and a stranger inherits the
        punishment.
      * merchant_id IS NULL OR merchant_id = %s   -- a NULL scope is global.
        A confirmed fraudulent card should not need blocking merchant by
        merchant.
    """
    if not entity_ids:
        return []
    cur = await conn.execute(
        """
        SELECT le.id, le.list_type, le.entity_id, le.merchant_id,
               le.reason, le.expires_at, e.entity_type
          FROM list_entries le
          JOIN entities e ON e.id = le.entity_id
         WHERE le.entity_id = ANY(%s)
           AND (le.merchant_id IS NULL OR le.merchant_id = %s)
           AND (le.expires_at IS NULL OR le.expires_at > now())
        """,
        (entity_ids, merchant_id),
    )
    return await cur.fetchall()


async def add_list_entry(
    conn: AsyncConnection,
    *,
    list_type: str,
    entity_id: UUID,
    merchant_id: UUID | None,
    reason: str,
    added_by: str,
    expires_at: Any = None,
) -> dict[str, Any]:
    cur = await conn.execute(
        """
        INSERT INTO list_entries (list_type, entity_id, merchant_id,
                                  reason, added_by, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (list_type, entity_id, merchant_id, reason, added_by, expires_at),
    )
    row = await cur.fetchone()
    assert row is not None
    return row
