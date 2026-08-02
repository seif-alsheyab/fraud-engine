"""API test helper.

HTTP requests cannot share the test's transaction: the app borrows its own
pool connection and would never see uncommitted rows. So these tests COMMIT
and clean up afterwards, scoped by a prefix unique to each suite.

That prefix discipline is not decoration. Pytest can run files in parallel,
and a shared prefix means one suite's cleanup deletes another's fixtures
mid-run -- producing "row vanished" failures and foreign-key violations that
look like several unrelated bugs.

NOTE on event loops: an async fixture declared scope="module" also needs
loop_scope="module" under pytest-asyncio 1.x. The fixture and the loop it
runs on must agree, or every test in the file errors at setup with a
ScopeMismatch. A module-scoped loop also means ONE pool for the file rather
than opening and closing it per test.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from httpx import ASGITransport, AsyncClient
from psycopg.rows import dict_row

from fraud_engine.api.app import create_app
from fraud_engine.config import get_settings
from fraud_engine.db.pool import close_pool, open_pool


@asynccontextmanager
async def api_client() -> AsyncIterator[AsyncClient]:
    """An httpx client wired straight to the ASGI app -- no network port.

    Opens and closes the pool itself. Suitable when a single test needs an
    isolated client; prefer the shared module fixture for whole suites.
    """
    app = create_app()
    await open_pool()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await close_pool()


@asynccontextmanager
async def shared_api_client() -> AsyncIterator[AsyncClient]:
    """Client for a module-scoped fixture: one pool for the whole file."""
    app = create_app()
    await open_pool()
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await close_pool()


@asynccontextmanager
async def direct_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    """A committing connection, for seeding and asserting outside the API."""
    settings = get_settings()
    conn = await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row, autocommit=True
    )
    try:
        yield conn
    finally:
        await conn.close()


async def cleanup_scope(scope: str) -> dict[str, int]:
    """Remove every row this scope created.

    Order is forced by the foreign keys: children before parents. The
    decisions table refuses DELETE by default, so the purge flag is set
    explicitly for this transaction only -- it disappears at COMMIT and
    cannot leak to another query.
    """
    settings = get_settings()
    like = f"{scope}%"
    removed: dict[str, int] = {}

    conn = await psycopg.AsyncConnection.connect(
        settings.database_url, row_factory=dict_row, autocommit=False
    )
    try:
        await conn.execute("SELECT set_config('fraud.allow_decision_purge','on',true)")
        scoped_txn = """
            SELECT t.id FROM transactions t
              JOIN merchants m ON m.id = t.merchant_id
             WHERE m.code LIKE %s"""
        steps = [
            ("review_cases", f"""DELETE FROM review_cases WHERE decision_id IN (
                 SELECT d.id FROM decisions d WHERE d.transaction_id IN ({scoped_txn}))"""),
            ("labels", f"DELETE FROM labels WHERE transaction_id IN ({scoped_txn})"),
            ("decisions", f"DELETE FROM decisions WHERE transaction_id IN ({scoped_txn})"),
            ("transactions", "DELETE FROM transactions WHERE merchant_id IN "
                             "(SELECT id FROM merchants WHERE code LIKE %s)"),
            ("rules", """DELETE FROM rules WHERE ruleset_id IN (
                 SELECT id FROM rulesets WHERE merchant_id IN
                   (SELECT id FROM merchants WHERE code LIKE %s))"""),
            ("list_entries", "DELETE FROM list_entries WHERE merchant_id IN "
                             "(SELECT id FROM merchants WHERE code LIKE %s)"),
            ("rulesets", "DELETE FROM rulesets WHERE merchant_id IN "
                         "(SELECT id FROM merchants WHERE code LIKE %s)"),
            ("merchants", "DELETE FROM merchants WHERE code LIKE %s"),
        ]
        for label, sql in steps:
            # No try/except: a cleanup that hides its own failures lets you
            # believe the database is clean when it is not.
            cur = await conn.execute(sql, (like,))
            removed[label] = cur.rowcount
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()

    return removed


async def seed_merchant_with_rules(scope: str) -> dict[str, Any]:
    """A merchant, an active ruleset, and a realistic set of rules."""
    from psycopg.types.json import Jsonb

    async with direct_conn() as conn:
        cur = await conn.execute(
            "INSERT INTO merchants (code, name, vertical, country, currency) "
            "VALUES (%s,%s,'DIGITAL','JO','USD') RETURNING *",
            (f"{scope}-M", f"{scope} Merchant"),
        )
        merchant = await cur.fetchone()

        cur = await conn.execute(
            "INSERT INTO rulesets (merchant_id, version, name, status, "
            "challenge_at, review_at, decline_at) "
            "VALUES (%s,1,'v1','ACTIVE',40,60,80) RETURNING *",
            (merchant["id"],),
        )
        ruleset = await cur.fetchone()

        rules = [
            ("VEL_CARD_1H", "Card used 4+ times in an hour",
             {"feature": "velocity_card_1h", "op": "gte", "value": 4}, 35, None),
            ("NO_CVV", "CVV did not match",
             {"feature": "cvv_match", "op": "eq", "value": False}, 25, None),
            ("THREE_DS", "3-D Secure authenticated",
             {"feature": "three_ds_status", "op": "eq", "value": "AUTHENTICATED"},
             -30, None),
            ("DENY", "On the deny list",
             {"feature": "on_deny_list", "op": "eq", "value": True}, 0, "DECLINE"),
            ("LINK_CARD", "Card seen on 3+ accounts",
             {"feature": "accounts_per_card_30d", "op": "gte", "value": 3}, 45, None),
        ]
        for code, name, cond, weight, hard in rules:
            await conn.execute(
                "INSERT INTO rules (ruleset_id, code, name, condition, weight, hard_action) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (ruleset["id"], code, name, Jsonb(cond), weight, hard),
            )

        return {"merchant": merchant, "ruleset": ruleset}
