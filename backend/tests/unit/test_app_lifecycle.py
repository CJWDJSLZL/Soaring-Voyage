from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
from app.config import Settings
from app.domain.memory import InMemoryRepository
from app.domain.postgres import PostgresIdentityProblemRepository
from app.main import create_app
from fastapi.testclient import TestClient

TENANT = "11111111-1111-4111-8111-111111111111"


class AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


def test_memory_lifecycle_keeps_single_store_as_identity_repository() -> None:
    configured = Settings(app_env="test", persistence_backend="memory")
    test_app = create_app(configured)

    with TestClient(test_app) as client:
        assert test_app.state.pool is None
        assert isinstance(test_app.state.store, InMemoryRepository)
        assert test_app.state.identity_repository is test_app.state.store
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["services"]["repository"] == "development-in-memory-adapter"
    assert response.json()["services"]["database"] == "not-wired"


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
    connection.fetchval.assert_awaited_with("SELECT 1")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["services"]["repository"] == "hybrid-postgres-identity-problems-assignments"
    assert body["services"]["database"] == "ok"
    assert body["services"]["sse_tickets"] == "development-in-memory-adapter"
    assert unported.status_code == 401


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
    assert response.json()["services"]["database"] == "unavailable"


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
