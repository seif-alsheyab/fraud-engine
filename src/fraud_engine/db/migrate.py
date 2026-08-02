"""Migration runner.

Applies every .sql file in migrations/ exactly once, in filename order, each
inside its own transaction. If any statement in a file fails, the whole file
rolls back and the migration is not recorded as applied -- so the database is
never left half-migrated.

A sha256 checksum of each file is stored. If an already-applied file is later
edited, the runner refuses to continue: the database no longer matches the
file, and silently ignoring that is how two environments drift apart until
something breaks in only one of them.
"""

import asyncio
import hashlib
import sys
from pathlib import Path

from fraud_engine.db.pool import close_pool, connection, open_pool

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def ensure_migrations_table() -> None:
    async with connection() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              filename   TEXT PRIMARY KEY,
              checksum   TEXT        NOT NULL,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            """
        )


async def run() -> int:
    await open_pool()
    try:
        await ensure_migrations_table()

        files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))

        async with connection() as conn:
            cur = await conn.execute("SELECT filename, checksum FROM schema_migrations")
            applied = {r["filename"]: r["checksum"] for r in await cur.fetchall()}

        count = 0
        for path in files:
            sql = path.read_text(encoding="utf-8")
            checksum = sha256(sql)

            if path.name in applied:
                if applied[path.name] != checksum:
                    raise RuntimeError(
                        f"Migration {path.name} was modified after being applied.\n"
                        f"Create a new migration instead of editing an applied one."
                    )
                print(f"  skip   {path.name}")
                continue

            async with connection() as conn:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                        (path.name, checksum),
                    )
            print(f"  apply  {path.name}")
            count += 1

        print("Database already up to date." if count == 0 else f"Applied {count} migration(s).")
        return 0
    finally:
        await close_pool()


def main() -> None:
    try:
        sys.exit(asyncio.run(run()))
    except Exception as exc:  # noqa: BLE001 - top-level reporter
        print(f"\nMIGRATION FAILED\n{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
