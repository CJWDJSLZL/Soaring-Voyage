from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from app.domain.models import User
from app.domain.postgres import NIL_SYSTEM_USER_ID, PostgresIdentityProblemRepository

TENANT = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"


class AsyncContext:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


def fake_pool(connection: Any) -> Any:
    connection.transaction = MagicMock(return_value=AsyncContext(None))
    pool = MagicMock()
    pool.acquire.return_value = AsyncContext(connection)
    return pool


def user_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": UUID(USER_ID),
        "username": "teacher",
        "display_name": "Teacher",
        "password_hash": "$2b$12$hash",
        "role": "teacher",
        "tenant_id": UUID(TENANT),
        "class_ids": [UUID("33333333-3333-4333-8333-333333333333")],
        "grade_level": None,
        "login_fail_count": 0,
        "locked_until": None,
        "token_version": 4,
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_login_lookup_uses_default_tenant_worker_context_nil_user_and_relational_classes() -> None:
    connection = AsyncMock()
    connection.fetchrow.return_value = user_row()
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)

    user = await repository.identity_by_username("teacher")

    assert user == User(
        USER_ID,
        "teacher",
        "Teacher",
        b"$2b$12$hash",
        "teacher",
        TENANT,
        ["33333333-3333-4333-8333-333333333333"],
        None,
        0,
        None,
        4,
    )
    context_call = connection.execute.await_args_list[0]
    assert context_call.args[2] == NIL_SYSTEM_USER_ID
    assert context_call.args[3] == "worker"
    sql, tenant_arg, username_arg = connection.fetchrow.await_args.args
    assert "u.tenant_id = $1" in sql
    assert "c.teacher_id = u.id" in sql
    assert "cs.student_id = u.id" in sql
    assert "ORDER BY c.id" in sql
    assert "ORDER BY cs.class_id" in sql
    assert tenant_arg == TENANT
    assert username_arg == "teacher"


@pytest.mark.asyncio
async def test_identity_by_id_explicitly_filters_tenant_and_uses_claimed_context() -> None:
    connection = AsyncMock()
    connection.fetchrow.return_value = user_row()
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)

    await repository.identity_by_id(USER_ID, TENANT, "teacher")

    sql, tenant_arg, user_arg = connection.fetchrow.await_args.args
    assert "u.tenant_id = $1" in sql
    assert "u.id = $2" in sql
    assert (tenant_arg, user_arg) == (TENANT, USER_ID)
    context_call = connection.execute.await_args_list[0]
    assert context_call.args[2:] == (USER_ID, "teacher")


@pytest.mark.asyncio
async def test_login_failure_and_token_updates_are_atomic() -> None:
    connection = AsyncMock()
    locked_until = datetime.now(UTC) + timedelta(minutes=15)
    connection.fetchrow.return_value = {"login_fail_count": 5, "locked_until": locked_until}
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    user = PostgresIdentityProblemRepository.user_from_row(user_row())

    updated = await repository.register_login_failure(user, max_failures=5, locked_until=locked_until)

    sql = connection.fetchrow.await_args.args[0]
    assert "login_fail_count = login_fail_count + 1" in sql
    assert "login_fail_count + 1 >= $3" in sql
    assert "tenant_id = $1" in sql
    assert updated.failed_logins == 5
    assert updated.locked_until == locked_until

    connection.fetchval.return_value = 5
    await repository.increment_token_version(user)
    token_sql = connection.fetchval.await_args.args[0]
    assert "token_version = token_version + 1" in token_sql
    assert "RETURNING token_version" in token_sql
    assert user.token_version == 5


@pytest.mark.asyncio
async def test_replace_password_updates_varchar_and_token_version_together() -> None:
    connection = AsyncMock()
    connection.fetchval.return_value = 8
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    user = PostgresIdentityProblemRepository.user_from_row(user_row())

    await repository.replace_password(user, b"new-hash")

    sql, tenant_arg, user_arg, password_arg = connection.fetchval.await_args.args
    assert "password_hash = $3" in sql
    assert "token_version = token_version + 1" in sql
    assert (tenant_arg, user_arg, password_arg) == (TENANT, USER_ID, "new-hash")
    assert user.password_hash == b"new-hash"
    assert user.token_version == 8


@pytest.mark.asyncio
async def test_problem_insert_and_list_are_tenant_scoped_and_deterministically_ordered() -> None:
    connection = AsyncMock()
    problem_id = UUID("44444444-4444-4444-8444-444444444444")
    connection.fetchval.side_effect = [problem_id, 1]
    created_at = datetime(2025, 1, 1, tzinfo=UTC)
    connection.fetch.return_value = [
        {
            "id": problem_id,
            "tenant_id": UUID(TENANT),
            "problem_type": "arithmetic",
            "grade_level": 3,
            "difficulty": "easy",
            "curriculum_version": "人教版",
            "problem_text": "1+1",
            "reference_answer": "2",
            "solution_steps": ["add"],
            "common_errors": [],
            "tags": ["addition"],
            "created_by": UUID(USER_ID),
            "created_at": created_at,
        }
    ]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    user = PostgresIdentityProblemRepository.user_from_row(user_row())
    payload = {
        "problem_type": "arithmetic",
        "grade_level": 3,
        "difficulty": "easy",
        "curriculum_version": "人教版",
        "problem_text": "1+1",
        "reference_answer": "2",
        "solution_steps": ["add"],
        "common_errors": [],
        "tags": ["addition"],
    }

    assert await repository.create_catalog_problem(user, payload) == str(problem_id)
    insert_sql = connection.fetchval.await_args_list[0].args[0]
    assert "INSERT INTO problems" in insert_sql
    assert "tenant_id" in insert_sql

    result = await repository.list_catalog_problems(
        user,
        grade_level=3,
        problem_type="arithmetic",
        difficulty="easy",
        keyword="1+",
        page_number=1,
        page_size=20,
    )
    list_sql = connection.fetch.await_args.args[0]
    count_sql = connection.fetchval.await_args_list[1].args[0]
    assert "tenant_id = $1" in list_sql
    assert "ORDER BY created_at DESC, id DESC" in list_sql
    assert "tenant_id = $1" in count_sql
    assert result["items"][0]["problem_id"] == str(problem_id)
    assert result["items"][0]["created_at"] == created_at.isoformat()
    assert result["total"] == 1
