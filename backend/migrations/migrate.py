"""Apply ordered SQL migrations under a PostgreSQL advisory lock."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg

_MIGRATIONS = Path(__file__).resolve().parent
_LOCK_ID = 7_260_721_001


async def migrate(dsn: str) -> list[str]:
    """Apply pending ``[0-9][0-9][0-9]_*.sql`` files and return their names."""
    connection = await asyncpg.connect(dsn)
    applied_now: list[str] = []
    try:
        await connection.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = await connection.fetch("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in rows}
        for path in sorted(_MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            if path.name in applied:
                continue
            async with connection.transaction():
                await connection.execute(path.read_text(encoding="utf-8"))
                await connection.execute("INSERT INTO schema_migrations(version) VALUES($1)", path.name)
            applied_now.append(path.name)
    finally:
        try:
            await connection.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
        finally:
            await connection.close()
    return applied_now


async def _main() -> None:
    dsn = os.environ.get("MIGRATION_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("MIGRATION_DATABASE_URL or DATABASE_URL is required")
    applied = await migrate(dsn)
    print(f"database migrations applied: {', '.join(applied) if applied else 'none'}")


if __name__ == "__main__":
    asyncio.run(_main())
