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

    def __post_init__(self) -> None:
        if not self.app_env:
            raise RuntimeError("APP_ENV must be explicitly set (test/development or a deployment environment)")
        if not self.api_prefix.startswith("/"):
            raise RuntimeError("API_PREFIX must start with '/'")
        if self.persistence_backend not in {"memory", "postgres"}:
            raise RuntimeError("PERSISTENCE_BACKEND must be 'memory' or 'postgres'")
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
            raise RuntimeError(
                "Production startup refused: the executable API still uses in-memory repository/ticket adapters; "
                "configure PostgreSQL/Redis adapters before selecting APP_ENV outside test/development"
            )
        if not self.jwt_secret:
            object.__setattr__(self, "jwt_secret", "phase1-development-secret-change-me")

    @property
    def is_development(self) -> bool:
        return self.app_env in _DEVELOPMENT_ENVS


settings = Settings()
