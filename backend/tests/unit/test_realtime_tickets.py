from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.models import Ticket
from app.realtime import MemoryTicketRepository, RedisTicketRepository


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.closed = False

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


def ticket(expires_at: datetime | None = None) -> Ticket:
    return Ticket(
        "user-1",
        "tenant-1",
        "submission-1",
        "student",
        expires_at or datetime.now(UTC) + timedelta(seconds=60),
    )


async def test_memory_ticket_repository_issues_consumes_once_and_purges_expired() -> None:
    tickets: dict[str, Ticket] = {
        "expired": ticket(datetime.now(UTC) - timedelta(seconds=1)),
    }
    repository = MemoryTicketRepository(tickets)

    value = await repository.issue(ticket(), ttl_seconds=60)

    assert "expired" not in tickets
    assert await repository.purge_expired() == 0
    consumed = await repository.consume(value)
    assert consumed is not None
    assert consumed.user_id == "user-1"
    assert await repository.consume(value) is None


async def test_redis_ticket_repository_serializes_and_consumes_once() -> None:
    fake: Any = FakeRedis()
    repository = RedisTicketRepository(fake)

    value = await repository.issue(ticket(), ttl_seconds=60)
    consumed = await repository.consume(value)

    assert consumed is not None
    assert consumed.user_id == "user-1"
    assert consumed.tenant_id == "tenant-1"
    assert consumed.submission_id == "submission-1"
    assert await repository.consume(value) is None
    assert await repository.ping() is True
    await repository.close()
    assert fake.closed is True
