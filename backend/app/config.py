from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import UUID

_DEVELOPMENT_ENVS = {"test", "development"}
_WEAK_SECRETS = {
    "",
    "change-me",
    "changeme",
    "phase1-development-secret-change-me",
    "CHANGE_ME_WITH_OPENSSL_RAND_HEX_32",
}


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in os.getenv(name, default).split(",") if value.strip())


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "").strip().lower())
    api_prefix: str = field(default_factory=lambda: os.getenv("API_PREFIX", "/api/v1").rstrip("/"))
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: _csv_env("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1")
    )
    allowed_hosts: tuple[str, ...] = field(
        default_factory=lambda: _csv_env("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
    )
    jwt_secret: str = field(default_factory=lambda: os.getenv("SECRET_KEY") or os.getenv("JWT_SECRET") or "")
    jwt_algorithm: str = "HS256"
    jwt_expires_seconds: int = 24 * 60 * 60
    bcrypt_rounds: int = 12
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    sse_ticket_ttl_seconds: int = 60
    persistence_backend: str = field(default_factory=lambda: os.getenv("PERSISTENCE_BACKEND", "memory").strip().lower())
    database_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL") or None)
    default_tenant_id: str | None = field(default_factory=lambda: os.getenv("DEFAULT_TENANT_ID") or None)
    redis_url: str | None = field(default_factory=lambda: os.getenv("REDIS_URL") or None)
    deepseek_api_key: str | None = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY") or None)
    llm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"))
    llm_primary_model: str = field(default_factory=lambda: os.getenv("LLM_PRIMARY_MODEL", "deepseek-v4-flash"))
    llm_fallback_model: str = field(default_factory=lambda: os.getenv("LLM_FALLBACK_MODEL", "deepseek-v4-pro"))
    max_llm_retries: int = field(default_factory=lambda: int(os.getenv("MAX_LLM_RETRIES", "3")))
    llm_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("LLM_TIMEOUT_SECONDS", "30")))
    use_mock_llm: bool = field(default_factory=lambda: _bool_env("USE_MOCK_LLM", False))

    def __post_init__(self) -> None:
        if not self.app_env:
            raise RuntimeError("APP_ENV must be explicitly set (test/development or a deployment environment)")
        if not self.api_prefix.startswith("/"):
            raise RuntimeError("API_PREFIX must start with '/'")
        if self.persistence_backend not in {"memory", "postgres"}:
            raise RuntimeError("PERSISTENCE_BACKEND must be 'memory' or 'postgres'")
        if self.max_llm_retries < 1:
            raise RuntimeError("MAX_LLM_RETRIES must be at least 1")
        if self.llm_timeout_seconds <= 0:
            raise RuntimeError("LLM_TIMEOUT_SECONDS must be positive")
        if self.persistence_backend == "postgres":
            if not self.database_url:
                raise RuntimeError("DATABASE_URL is required when PERSISTENCE_BACKEND=postgres")
            try:
                normalized_tenant = str(UUID(self.default_tenant_id or ""))
            except (ValueError, TypeError, AttributeError) as exc:
                raise RuntimeError("DEFAULT_TENANT_ID must be a valid UUID when PERSISTENCE_BACKEND=postgres") from exc
            object.__setattr__(self, "default_tenant_id", normalized_tenant)
        if self.app_env not in _DEVELOPMENT_ENVS:
            if len(self.jwt_secret) < 32 or self.jwt_secret in _WEAK_SECRETS:
                raise RuntimeError(
                    "SECRET_KEY must be a strong value of at least 32 characters outside test/development"
                )
            if self.persistence_backend != "postgres" or not self.redis_url:
                raise RuntimeError(
                    "Production startup refused: configure PostgreSQL and Redis adapters before selecting "
                    "APP_ENV outside test/development"
                )
            raise RuntimeError(
                "Production startup refused: Qdrant/RAG and deployment gates remain not wired for production"
            )
        if not self.jwt_secret:
            object.__setattr__(self, "jwt_secret", "phase1-development-secret-change-me")

    @property
    def is_development(self) -> bool:
        return self.app_env in _DEVELOPMENT_ENVS


settings = Settings()
