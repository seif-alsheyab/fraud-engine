"""Shared async connection pool.

Opening a Postgres connection is expensive: TCP handshake, authentication,
and a backend process spawned server-side. A pool keeps a small set open and
lends them out, so a request borrows one, uses it, and returns it.

The pool is created but NOT opened at import time. Opening performs I/O, and
doing I/O as a side effect of importing a module makes the module impossible
to import in a context without a running event loop -- including some test
collection paths.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from fraud_engine.config import get_settings

_pool: AsyncConnectionPool | None = None


def _make_pool() -> AsyncConnectionPool:
    settings = get_settings()
    return AsyncConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=10,
        # Fail fast rather than queueing forever when the database is down.
        # A request that hangs is worse than one that errors: the caller's
        # own timeout fires and the connection is wasted anyway.
        timeout=5.0,
        max_idle=30.0,
        # dict_row makes every query return dicts instead of tuples, so
        # callers read row["decision"] rather than row[4] -- which silently
        # breaks the moment a column is added to the SELECT.
        kwargs={"row_factory": dict_row},
        # open=False is required: opening in the constructor is deprecated
        # in psycopg_pool 3.2+ and warns on every start.
        open=False,
    )


async def open_pool() -> AsyncConnectionPool:
    """Create and open the pool. Safe to call more than once."""
    global _pool
    if _pool is None:
        _pool = _make_pool()
    if _pool.closed:
        _pool = _make_pool()
    await _pool.open()
    await _pool.wait(timeout=10.0)
    return _pool


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Connection pool is not open. Call open_pool() first.")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def connection() -> AsyncIterator[AsyncConnection]:
    """Borrow a connection. Commits on success, rolls back on exception."""
    pool = get_pool()
    async with pool.connection() as conn:
        yield conn
