from __future__ import annotations

from uuid import UUID

import pytest
from app.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("PERSISTENCE_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)


def test_memory_is_safe_default_and_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    configured = Settings()
    assert configured.persistence_backend == "memory"
    assert configured.database_url is None
    assert configured.default_tenant_id is None


def test_postgres_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "11111111-1111-4111-8111-111111111111")
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        Settings()


def test_postgres_requires_valid_default_tenant_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql:///app")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "tenant-demo")
    with pytest.raises(RuntimeError, match="DEFAULT_TENANT_ID"):
        Settings()


def test_postgres_configuration_is_accepted_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql:///app")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "11111111-1111-4111-8111-111111111111")
    configured = Settings()
    assert configured.persistence_backend == "postgres"
    assert configured.default_tenant_id == str(UUID("11111111-1111-4111-8111-111111111111"))


def test_unknown_persistence_backend_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")
    with pytest.raises(RuntimeError, match="PERSISTENCE_BACKEND"):
        Settings()
