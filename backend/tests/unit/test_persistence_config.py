from __future__ import annotations

from uuid import UUID

import pytest
from app.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("PERSISTENCE_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEFAULT_TENANT_ID", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)


def _production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql:///app")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://school.example")
    monkeypatch.setenv("ALLOWED_HOSTS", "api.school.example")


def test_memory_is_safe_default_and_needs_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    configured = Settings()
    assert configured.persistence_backend == "memory"
    assert configured.database_url is None
    assert configured.default_tenant_id is None
    assert configured.llm_primary_model == "deepseek-v4-flash"
    assert configured.llm_fallback_model == "deepseek-v4-pro"


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


def test_llm_configuration_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "primary")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "fallback")
    monkeypatch.setenv("MAX_LLM_RETRIES", "2")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("USE_MOCK_LLM", "true")

    configured = Settings()

    assert configured.deepseek_api_key == "test-key"
    assert configured.llm_base_url == "https://example.invalid/v1"
    assert configured.llm_primary_model == "primary"
    assert configured.llm_fallback_model == "fallback"
    assert configured.max_llm_retries == 2
    assert configured.llm_timeout_seconds == 5
    assert configured.use_mock_llm is True


def test_production_configuration_is_accepted_when_required_adapters_are_wired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _production_env(monkeypatch)

    configured = Settings()

    assert configured.app_env == "production"
    assert configured.persistence_backend == "postgres"
    assert configured.redis_url == "redis://localhost:6379/0"
    assert configured.allowed_origins == ("https://school.example",)
    assert configured.allowed_hosts == ("api.school.example",)


def test_production_requires_explicit_origins_and_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    _production_env(monkeypatch)
    monkeypatch.delenv("ALLOWED_ORIGINS")

    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS"):
        Settings()

    _production_env(monkeypatch)
    monkeypatch.setenv("ALLOWED_HOSTS", "localhost")

    with pytest.raises(RuntimeError, match="ALLOWED_HOSTS"):
        Settings()
