"""Request-local tenant identity and transaction-scoped RLS context."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID

import asyncpg


class TenantContextError(RuntimeError):
    """Raised when a tenant transaction is opened without trusted identity."""


@dataclass(frozen=True, slots=True)
class TenantIdentity:
    tenant_id: str
    role: str


_identity: ContextVar[TenantIdentity | None] = ContextVar("tenant_identity", default=None)
_ALLOWED_ROLES = frozenset({"student", "teacher", "admin", "sysadmin", "worker"})


def _uuid(value: str, field: str) -> str:
    try:
        return str(UUID(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise TenantContextError(f"invalid {field}") from exc


@contextmanager
def tenant_context(tenant_id: str, role: str) -> Iterator[TenantIdentity]:
    """Bind authenticated tenant identity to the current async task."""
    normalized_tenant = _uuid(tenant_id, "tenant_id")
    if role not in _ALLOWED_ROLES:
        raise TenantContextError("invalid tenant role")
    identity = TenantIdentity(normalized_tenant, role)
    token: Token[TenantIdentity | None] = _identity.set(identity)
    try:
        yield identity
    finally:
        _identity.reset(token)


def current_tenant() -> TenantIdentity:
    """Return current identity, failing closed outside authenticated requests."""
    identity = _identity.get()
    if identity is None:
        raise TenantContextError("tenant context is not set")
    return identity


@asynccontextmanager
async def tenant_conn(
    pool: asyncpg.Pool,
    *,
    user_id: str,
) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a transaction and install parameterized transaction-local RLS GUCs."""
    identity = current_tenant()
    normalized_user = _uuid(user_id, "user_id")
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                "SELECT set_config('app.current_tenant_id', $1, true), "
                "set_config('app.current_user_id', $2, true), "
                "set_config('app.current_role', $3, true)",
                identity.tenant_id,
                normalized_user,
                identity.role,
            )
            yield connection
