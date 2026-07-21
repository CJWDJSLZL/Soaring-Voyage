"""Contract tests for the asyncpg boundary and initial PostgreSQL migration."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.db.pool import PoolSettings, close_pool, create_pool
from app.db.session import TenantContextError, tenant_conn, tenant_context


class _AcquireContext:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    async def __aenter__(self) -> Any:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _TransactionContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_create_pool_applies_production_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    created_pool = object()
    create_pool_mock = AsyncMock(return_value=created_pool)
    monkeypatch.setattr("app.db.pool.asyncpg.create_pool", create_pool_mock)

    result = await create_pool("postgresql://user:secret@db/app")

    assert result is created_pool
    create_pool_mock.assert_awaited_once_with(
        dsn="postgresql://user:secret@db/app",
        min_size=5,
        max_size=25,
        command_timeout=30.0,
        max_inactive_connection_lifetime=300.0,
        server_settings={"application_name": "soaring-voyage"},
    )


@pytest.mark.asyncio
async def test_create_pool_accepts_typed_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    create_pool_mock = AsyncMock(return_value=object())
    monkeypatch.setattr("app.db.pool.asyncpg.create_pool", create_pool_mock)
    settings = PoolSettings(min_size=1, max_size=3, command_timeout=4, inactive_lifetime=5)

    await create_pool("postgresql://db/test", settings=settings)

    assert create_pool_mock.await_args.kwargs["min_size"] == 1
    assert create_pool_mock.await_args.kwargs["max_size"] == 3
    assert create_pool_mock.await_args.kwargs["command_timeout"] == 4
    assert create_pool_mock.await_args.kwargs["max_inactive_connection_lifetime"] == 5


@pytest.mark.asyncio
async def test_close_pool_is_safe_and_awaits_close() -> None:
    pool = AsyncMock()
    await close_pool(pool)
    pool.close.assert_awaited_once_with()
    await close_pool(None)


@pytest.mark.asyncio
async def test_tenant_connection_sets_transaction_local_rls_context() -> None:
    connection = AsyncMock()
    connection.transaction = MagicMock(return_value=_TransactionContext())
    pool = MagicMock()
    pool.acquire.return_value = _AcquireContext(connection)

    with tenant_context("11111111-1111-4111-8111-111111111111", "teacher"):
        async with tenant_conn(pool, user_id="22222222-2222-4222-8222-222222222222") as yielded:
            assert yielded is connection

    connection.execute.assert_awaited_once_with(
        "SELECT set_config('app.current_tenant_id', $1, true), "
        "set_config('app.current_user_id', $2, true), "
        "set_config('app.current_role', $3, true)",
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "teacher",
    )


@pytest.mark.asyncio
async def test_tenant_context_is_required() -> None:
    with pytest.raises(TenantContextError, match="tenant"):
        async with tenant_conn(AsyncMock(), user_id="22222222-2222-4222-8222-222222222222"):
            pass


def test_initial_migration_contains_required_schema_and_rls() -> None:
    sql = (Path(__file__).parents[2] / "migrations" / "001_initial_schema.sql").read_text()
    required_tables = {
        "tenants",
        "users",
        "classes",
        "class_students",
        "problems",
        "assignments",
        "assignment_classes",
        "assignment_problems",
        "submissions",
        "submission_answers",
        "grading_results",
        "human_review_queue",
        "student_error_history",
        "harness_runs",
        "jobs",
        "audit_logs",
    }
    lowered = sql.lower()
    for table in required_tables:
        assert f"create table {table}" in lowered
    assert "enable row level security" in lowered
    assert "force row level security" in lowered
    assert "current_setting('app.current_tenant_id', true)" in lowered
    assert "create policy audit_insert_policy" in lowered
    assert "create policy audit_select_policy" in lowered
    assert "create trigger student_error_history_problem_tenant" in lowered
    assert "create trigger student_error_history_grading_tenant" in lowered
    assert "unique (tenant_id, username)" in lowered
    assert "unique (assignment_id, problem_id)" in lowered
    assert "unique (student_id, assignment_id)" in lowered
    assert "create function enforce_problem_tenant" in lowered
    assert "on conflict (grading_result_id) do update" in lowered
    assert "grant usage on schema public to soaring_voyage_app" in lowered


def test_initial_migration_bootstraps_runtime_role_before_grants() -> None:
    sql = (Path(__file__).parents[2] / "migrations" / "001_initial_schema.sql").read_text().lower()

    role_guard = "select 1 from pg_roles where rolname = 'soaring_voyage_app'"
    role_create = "create role soaring_voyage_app"
    first_grant = "grant usage on schema public to soaring_voyage_app"
    assert role_guard in sql
    assert role_create in sql
    assert sql.index(role_create) < sql.index(first_grant)
