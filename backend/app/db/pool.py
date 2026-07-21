"""Typed asyncpg connection-pool lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


@dataclass(frozen=True, slots=True)
class PoolSettings:
    """Pool tuning defaults for an 8-core single-school deployment."""

    min_size: int = 5
    max_size: int = 25
    command_timeout: float = 30.0
    inactive_lifetime: float = 300.0
    application_name: str = "soaring-voyage"

    def __post_init__(self) -> None:
        if self.min_size < 0:
            raise ValueError("min_size must be non-negative")
        if self.max_size < 1 or self.min_size > self.max_size:
            raise ValueError("max_size must be positive and >= min_size")
        if self.command_timeout <= 0 or self.inactive_lifetime < 0:
            raise ValueError("pool timeouts must be positive")


async def create_pool(
    dsn: str,
    *,
    settings: PoolSettings | None = None,
) -> asyncpg.Pool:
    """Create the process-wide pool; callers own and must close the result."""
    if not dsn:
        raise ValueError("database DSN must not be empty")
    config = settings or PoolSettings()
    return await asyncpg.create_pool(
        dsn=dsn,
        min_size=config.min_size,
        max_size=config.max_size,
        command_timeout=config.command_timeout,
        max_inactive_connection_lifetime=config.inactive_lifetime,
        server_settings={"application_name": config.application_name},
    )


async def close_pool(pool: asyncpg.Pool | None) -> None:
    """Close a pool during application shutdown; tolerate partial startup."""
    if pool is not None:
        await pool.close()
