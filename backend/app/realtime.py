from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from redis.asyncio import Redis

from app.core.errors import utcnow
from app.domain.models import Ticket


class TicketRepository(Protocol):
    backend_name: str

    async def issue(self, ticket: Ticket, ttl_seconds: int) -> str: ...
    async def consume(self, value: str) -> Ticket | None: ...
    async def purge_expired(self) -> int: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryTicketRepository:
    backend_name = "development-in-memory-adapter"

    def __init__(self, tickets: dict[str, Ticket]) -> None:
        self._tickets = tickets

    async def issue(self, ticket: Ticket, ttl_seconds: int) -> str:
        self.purge_expired_sync()
        value = secrets.token_urlsafe(32)
        ticket.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        self._tickets[value] = ticket
        return value

    async def consume(self, value: str) -> Ticket | None:
        self.purge_expired_sync()
        ticket = self._tickets.pop(value, None)
        if ticket is None or ticket.expires_at <= utcnow():
            return None
        return ticket

    async def purge_expired(self) -> int:
        return self.purge_expired_sync()

    def purge_expired_sync(self) -> int:
        expired = [value for value, ticket in self._tickets.items() if ticket.expires_at <= utcnow()]
        for value in expired:
            self._tickets.pop(value, None)
        return len(expired)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class RedisTicketRepository:
    backend_name = "redis"

    def __init__(self, client: Redis, *, prefix: str = "sse-ticket") -> None:
        self._client = client
        self._prefix = prefix

    async def issue(self, ticket: Ticket, ttl_seconds: int) -> str:
        ticket.expires_at = utcnow() + timedelta(seconds=ttl_seconds)
        payload = json.dumps(
            {
                "user_id": ticket.user_id,
                "tenant_id": ticket.tenant_id,
                "submission_id": ticket.submission_id,
                "role": ticket.role,
                "expires_at": ticket.expires_at.isoformat(),
            },
            ensure_ascii=False,
        )
        for _ in range(3):
            value = secrets.token_urlsafe(32)
            created = await self._client.set(self._key(value), payload, ex=ttl_seconds, nx=True)
            if created:
                return value
        raise RuntimeError("Could not allocate a unique SSE ticket")

    async def consume(self, value: str) -> Ticket | None:
        raw = await self._client.getdel(self._key(value))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data: dict[str, Any] = json.loads(raw)
        ticket = Ticket(
            data["user_id"],
            data["tenant_id"],
            data["submission_id"],
            data["role"],
            datetime.fromisoformat(data["expires_at"]),
        )
        if ticket.expires_at <= utcnow():
            return None
        return ticket

    async def purge_expired(self) -> int:
        return 0

    async def ping(self) -> bool:
        try:
            ping_result = cast(Awaitable[Any], self._client.ping())
            return bool(await ping_result)
        except Exception:
            return False

    async def close(self) -> None:
        close_result = cast(Awaitable[Any], self._client.aclose())
        await close_result

    def _key(self, value: str) -> str:
        return f"{self._prefix}:{value}"
