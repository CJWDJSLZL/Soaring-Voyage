from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
from app.config import Settings
from app.domain.memory import InMemoryRepository
from app.domain.postgres import PostgresIdentityProblemRepository
from app.main import create_app
from app.realtime import RedisTicketRepository
from fastapi.testclient import TestClient

TENANT = "11111111-1111-4111-8111-111111111111"


class AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


class FakeRedis:
    def __init__(self) -> None:
        self.closed = False

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class FakeRagIndexer:
    status = "qdrant-configured"

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def service(body: dict[str, Any], name: str) -> dict[str, Any]:
    return body["services"][name]


def test_memory_lifecycle_keeps_single_store_as_identity_repository() -> None:
    configured = Settings(app_env="test", persistence_backend="memory")
    test_app = create_app(configured)

    with TestClient(test_app) as client:
        assert test_app.state.pool is None
        assert isinstance(test_app.state.store, InMemoryRepository)
        assert test_app.state.identity_repository is test_app.state.store
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == "1.0.0"
    assert body["uptime_seconds"] >= 0
    assert body["grading"] == {"active_requests": 0, "pending_hitl_count": 0}
    assert service(body, "repository") == {
        "status": "ok",
        "latency_ms": None,
        "backend": "development-in-memory-adapter",
    }
    assert service(body, "database") == {"status": "not-wired", "latency_ms": None, "backend": "memory"}
    assert service(body, "qdrant") == {"status": "not-wired", "latency_ms": None, "backend": "local-metadata-index"}


def test_postgres_pool_repository_health_and_shutdown_lifecycle() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = 1
    pool = MagicMock()
    pool.acquire.return_value = AsyncContext(connection)
    pool.close = AsyncMock()
    pool_factory = AsyncMock(return_value=pool)
    configured = Settings(
        app_env="test",
        persistence_backend="postgres",
        database_url="postgresql:///app",
        default_tenant_id=TENANT,
    )
    test_app = create_app(configured, pool_factory=pool_factory)

    with TestClient(test_app) as client:
        assert test_app.state.pool is pool
        assert isinstance(test_app.state.store, InMemoryRepository)
        assert isinstance(test_app.state.identity_repository, PostgresIdentityProblemRepository)
        response = client.get("/health")
        unported = client.get("/api/v1/assignments/")
        assert pool.close.await_count == 0

    pool_factory.assert_awaited_once_with("postgresql:///app")
    pool.close.assert_awaited_once_with()
    assert connection.fetchval.await_args_list[0].args == ("SELECT 1",)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["grading"] == {"active_requests": 0, "pending_hitl_count": 0}
    assert service(body, "repository")["backend"] == "hybrid-postgres-identity-problems-assignments"
    assert service(body, "database")["status"] == "ok"
    assert service(body, "database")["latency_ms"] >= 0
    assert service(body, "sse_tickets")["status"] == "ok"
    assert service(body, "sse_tickets")["backend"] == "development-in-memory-adapter"
    assert unported.status_code == 401


def test_redis_ticket_repository_health_and_shutdown_lifecycle() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = 1
    pool = MagicMock()
    pool.acquire.return_value = AsyncContext(connection)
    pool.close = AsyncMock()
    fake_redis = FakeRedis()
    configured = Settings(
        app_env="test",
        persistence_backend="postgres",
        database_url="postgresql:///app",
        default_tenant_id=TENANT,
        redis_url="redis://localhost:6379/0",
    )
    test_app = create_app(
        configured,
        pool_factory=AsyncMock(return_value=pool),
        redis_factory=MagicMock(return_value=fake_redis),
    )

    with TestClient(test_app) as client:
        assert isinstance(test_app.state.ticket_repository, RedisTicketRepository)
        response = client.get("/health")
        assert fake_redis.closed is False

    body = response.json()
    assert service(body, "sse_tickets")["backend"] == "redis"
    assert service(body, "redis")["status"] == "ok"
    assert service(body, "redis")["latency_ms"] >= 0
    assert fake_redis.closed is True


def test_production_lifecycle_uses_external_adapters_without_memory_store() -> None:
    connection = AsyncMock()
    connection.transaction = MagicMock(return_value=AsyncContext(None))
    connection.fetchval.side_effect = [1, 2]
    pool = MagicMock()
    pool.acquire.return_value = AsyncContext(connection)
    pool.close = AsyncMock()
    fake_redis = FakeRedis()
    fake_rag = FakeRagIndexer()
    configured = Settings(
        app_env="production",
        persistence_backend="postgres",
        database_url="postgresql:///app",
        default_tenant_id=TENANT,
        redis_url="redis://localhost:6379/0",
        qdrant_url="http://qdrant:6333",
        jwt_secret="x" * 40,
        allowed_origins=("https://school.example",),
        allowed_hosts=("api.school.example",),
        allowed_origins_configured=True,
        allowed_hosts_configured=True,
    )
    test_app = create_app(
        configured,
        pool_factory=AsyncMock(return_value=pool),
        redis_factory=MagicMock(return_value=fake_redis),
        qdrant_indexer_factory=MagicMock(return_value=fake_rag),
    )

    with TestClient(test_app) as client:
        assert test_app.state.store is None
        assert isinstance(test_app.state.identity_repository, PostgresIdentityProblemRepository)
        assert isinstance(test_app.state.ticket_repository, RedisTicketRepository)
        response = client.get("/health", headers={"Host": "api.school.example"})

    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "production"
    assert service(body, "database")["status"] == "ok"
    assert body["grading"] == {"active_requests": 0, "pending_hitl_count": 2}
    assert service(body, "redis")["status"] == "ok"
    assert service(body, "qdrant") == {"status": "ok", "latency_ms": None, "backend": "qdrant-configured"}
    context_sql, tenant_arg, user_arg, role_arg = connection.execute.await_args.args
    assert "app.current_tenant_id" in context_sql
    assert (tenant_arg, user_arg, role_arg) == (
        TENANT,
        "00000000-0000-0000-0000-000000000000",
        "worker",
    )
    pool.close.assert_awaited_once_with()
    assert fake_redis.closed is True
    assert fake_rag.closed is True


def test_postgres_health_reports_failed_ping() -> None:
    connection = AsyncMock()
    connection.fetchval.side_effect = RuntimeError("database unavailable")
    pool = MagicMock()
    pool.acquire.return_value = AsyncContext(connection)
    pool.close = AsyncMock()
    configured = Settings(
        app_env="test",
        persistence_backend="postgres",
        database_url="postgresql:///app",
        default_tenant_id=TENANT,
    )
    test_app = create_app(configured, pool_factory=AsyncMock(return_value=pool))

    with TestClient(test_app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert service(response.json(), "database") == {"status": "unavailable", "latency_ms": None}


def test_application_factory_uses_supplied_jwt_settings() -> None:
    configured = Settings(
        app_env="test",
        persistence_backend="memory",
        jwt_secret="factory-specific-secret-with-at-least-32-characters",
    )
    test_app = create_app(configured)

    with TestClient(test_app) as client:
        response = client.post(
            "/api/v1/auth/login",
            json={"username": "student", "password": "Test@1234"},
        )

    assert response.status_code == 200
    token = response.json()["data"]["access_token"]
    claims = jwt.decode(token, configured.jwt_secret, algorithms=[configured.jwt_algorithm])
    assert claims["user_id"] == "user-student"
