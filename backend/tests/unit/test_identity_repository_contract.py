from __future__ import annotations

from datetime import timedelta

import pytest
from app.core.errors import utcnow
from app.core.security import hash_password, verify_password
from app.domain.memory import InMemoryRepository


def test_invalid_or_missing_password_hash_is_not_a_server_error() -> None:
    assert verify_password("anything", b"") is False
    assert verify_password("anything", b"not-a-bcrypt-hash") is False


@pytest.mark.asyncio
async def test_memory_repository_implements_async_identity_lifecycle() -> None:
    repository = InMemoryRepository()

    user = await repository.identity_by_username("student")
    assert user is not None
    assert await repository.identity_by_id(user.user_id, user.tenant_id, user.role) is user
    assert await repository.identity_by_id(user.user_id, "other-tenant", user.role) is None

    locked_at = utcnow() + timedelta(minutes=15)
    for expected in range(1, 6):
        updated = await repository.register_login_failure(user, max_failures=5, locked_until=locked_at)
        assert updated.failed_logins == expected
    assert updated.locked_until == locked_at

    await repository.clear_login_failures(user)
    assert user.failed_logins == 0
    assert user.locked_until is None

    old_version = user.token_version
    await repository.replace_password(user, hash_password("Different@123"))
    assert user.token_version == old_version + 1
    await repository.increment_token_version(user)
    assert user.token_version == old_version + 2


@pytest.mark.asyncio
async def test_memory_repository_problem_contract_is_tenant_scoped_filtered_and_ordered() -> None:
    repository = InMemoryRepository()
    teacher = await repository.identity_by_username("teacher")
    assert teacher is not None
    payload = {
        "problem_text": "Second fraction question",
        "problem_type": "arithmetic",
        "reference_answer": "2",
        "grade_level": 3,
        "difficulty": "medium",
        "curriculum_version": "人教版",
        "solution_steps": ["step"],
        "common_errors": [],
        "tags": ["fraction"],
    }
    second_id = await repository.create_catalog_problem(teacher, payload)
    first_payload = {**payload, "problem_text": "First fraction question", "difficulty": "easy"}
    first_id = await repository.create_catalog_problem(teacher, first_payload)
    # Make ordering independent from insertion order.
    repository.problems[first_id]["created_at"] = "2025-01-01T00:00:00+00:00"
    repository.problems[second_id]["created_at"] = "2025-01-02T00:00:00+00:00"
    repository.problems["foreign"] = {**payload, "problem_id": "foreign", "tenant_id": "foreign"}

    result = await repository.list_catalog_problems(
        teacher,
        grade_levels=[3],
        problem_type="arithmetic",
        difficulty=None,
        keyword="FRACTION",
        tags=["fraction"],
        source="school",
        page_number=1,
        page_size=1,
    )

    assert result["total"] == 2
    assert result["items"][0]["problem_id"] == second_id
    assert result["has_next"] is True
