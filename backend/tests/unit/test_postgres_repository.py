from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from app.core.errors import AppError
from app.domain.models import User
from app.domain.postgres import NIL_SYSTEM_USER_ID, PostgresIdentityProblemRepository

TENANT = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"
CLASS_ID = "33333333-3333-4333-8333-333333333333"
ASSIGNMENT_ID = "55555555-5555-4555-8555-555555555555"
PROBLEM_ID = "66666666-6666-4666-8666-666666666666"


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


class FakeRagIndexer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    async def upsert_problems(self, tenant_id: str, problems: list[dict[str, Any]]) -> int:
        self.calls.append((tenant_id, problems))
        return len(problems)


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
        "force_change_password": False,
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
        False,
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
async def test_record_logout_writes_audit_log() -> None:
    connection = AsyncMock()
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    user = PostgresIdentityProblemRepository.user_from_row(user_row())

    await repository.record_logout(user)

    business_calls = [call.args for call in connection.execute.await_args_list if "set_config" not in call.args[0]]
    audit_sql, tenant_arg, operator_arg, resource_arg = business_calls[0]
    assert "INSERT INTO audit_logs" in audit_sql
    assert "'LOGOUT'" in audit_sql
    assert "'auth'" in audit_sql
    assert (tenant_arg, operator_arg, resource_arg) == (TENANT, USER_ID, USER_ID)


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
    assert "force_change_password = false" in sql
    assert (tenant_arg, user_arg, password_arg) == (TENANT, USER_ID, "new-hash")
    assert user.password_hash == b"new-hash"
    assert user.token_version == 8


@pytest.mark.asyncio
async def test_bulk_create_students_inserts_users_class_links_and_audit_summary() -> None:
    connection = AsyncMock()
    student_id = UUID("77777777-7777-4777-8777-777777777777")
    existing_id = UUID("88888888-8888-4888-8888-888888888888")
    connection.fetch.return_value = [{"id": UUID(CLASS_ID), "name": "三年级A班", "grade_level": 3}]
    connection.fetchval.side_effect = [None, student_id, existing_id]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin", class_ids=[]))

    result = await repository.bulk_create_students(
        admin,
        [
            {
                "row": 2,
                "display_name": "张三",
                "username": "zhangsan",
                "password_hash": b"hash-1",
                "grade_level": 3,
                "class_name": "三年级A班",
            },
            {
                "row": 3,
                "display_name": "李四",
                "username": "lisi",
                "password_hash": b"hash-2",
                "grade_level": 3,
                "class_name": "三年级A班",
            },
            {
                "row": 4,
                "display_name": "王五",
                "username": "wangwu",
                "password_hash": b"hash-3",
                "grade_level": 3,
                "class_name": "不存在班级",
            },
        ],
    )

    assert result["created"] == 1
    assert result["skipped"] == 1
    assert result["failed"] == 1
    executed_sql = "\n".join(call.args[0] for call in connection.execute.await_args_list)
    assert "INSERT INTO class_students" in executed_sql
    assert "INSERT INTO audit_logs" in executed_sql
    inserted_sql = connection.fetchval.await_args_list[1].args[0]
    assert "INSERT INTO users" in inserted_sql
    assert connection.fetchval.await_args_list[1].args[4] == "hash-1"


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


@pytest.mark.asyncio
async def test_bulk_import_problems_inserts_rows_and_completed_job() -> None:
    connection = AsyncMock()
    first_id = UUID("77777777-7777-4777-8777-777777777777")
    second_id = UUID("88888888-8888-4888-8888-888888888888")
    job_id = UUID("99999999-9999-4999-8999-999999999999")
    connection.fetchval.side_effect = [first_id, second_id]
    connection.fetchrow.return_value = {"id": job_id, "status": "succeeded"}
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    result = await repository.bulk_import_problems(
        teacher,
        [
            {
                "row": 2,
                "problem_text": "1 + 1 = ___",
                "problem_type": "arithmetic",
                "reference_answer": "2",
                "grade_level": 1,
                "difficulty": "easy",
                "solution_steps": ["add"],
                "common_errors": [],
                "tags": ["addition"],
            },
            {
                "row": 3,
                "problem_text": "选择题",
                "problem_type": "multiple_choice",
                "reference_answer": "B",
                "grade_level": 1,
                "difficulty": "easy",
                "solution_steps": [],
                "common_errors": [],
                "tags": ["choice"],
            },
        ],
        {"curriculum_version": "renjiao"},
    )

    assert result["import_job_id"] == str(job_id)
    assert result["status"] == "succeeded"
    assert result["success"] == 2
    assert result["problem_ids"] == [str(first_id), str(second_id)]
    insert_sql = connection.fetchval.await_args_list[0].args[0]
    assert "INSERT INTO problems" in insert_sql
    job_sql = connection.fetchrow.await_args.args[0]
    assert "bulk_import_problems" in job_sql


@pytest.mark.asyncio
async def test_delete_problem_soft_deletes_unreferenced_teacher_problem() -> None:
    connection = AsyncMock()
    connection.fetchrow.return_value = {"id": UUID(PROBLEM_ID), "created_by": UUID(USER_ID)}
    connection.fetch.return_value = []
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    result = await repository.delete_catalog_problem(teacher, PROBLEM_ID)

    assert result == {"problem_id": PROBLEM_ID, "deleted": True}
    update_sql = connection.execute.await_args_list[-1].args[0]
    assert "UPDATE problems" in update_sql
    assert "is_deleted = true" in update_sql


@pytest.mark.asyncio
async def test_delete_problem_blocks_foreign_or_referenced_problem() -> None:
    connection = AsyncMock()
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    connection.fetchrow.return_value = {
        "id": UUID(PROBLEM_ID),
        "created_by": UUID("99999999-9999-4999-8999-999999999999"),
    }
    with pytest.raises(AppError) as forbidden:
        await repository.delete_catalog_problem(teacher, PROBLEM_ID)
    assert forbidden.value.status_code == 403

    connection.reset_mock()
    connection.fetchrow.return_value = {"id": UUID(PROBLEM_ID), "created_by": UUID(USER_ID)}
    connection.fetch.return_value = [{"assignment_id": UUID(ASSIGNMENT_ID)}]
    with pytest.raises(AppError) as conflict:
        await repository.delete_catalog_problem(teacher, PROBLEM_ID)
    assert conflict.value.status_code == 409
    assert not any("UPDATE problems" in call.args[0] for call in connection.execute.await_args_list)


@pytest.mark.asyncio
async def test_submit_assignment_persists_answer_grading_and_review_queue() -> None:
    connection = AsyncMock()
    submitted_at = datetime(2026, 1, 1, tzinfo=UTC)
    updated_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    submission_id = UUID("77777777-7777-4777-8777-777777777777")
    grading_id = UUID("88888888-8888-4888-8888-888888888888")
    connection.fetchrow.side_effect = [
        {
            "id": UUID(ASSIGNMENT_ID),
            "title": "A",
            "due_date": None,
            "class_ids": [UUID(CLASS_ID)],
        },
        {"id": submission_id, "submitted_at": submitted_at, "updated_at": submitted_at},
    ]
    connection.fetchval.side_effect = [None, grading_id, updated_at]
    connection.fetch.return_value = [
        {
            "id": UUID(PROBLEM_ID),
            "problem_text": "1 + 1 = ___",
            "problem_type": "arithmetic",
            "grade_level": 3,
            "difficulty": "easy",
            "reference_answer": "2",
            "solution_steps": ["add"],
            "common_errors": [],
            "tags": ["加法"],
            "position": 1,
        }
    ]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    student = User(USER_ID, "student", "Student", b"hash", "student", TENANT, [CLASS_ID], 3)

    async def grade_problem(_problem, answer):
        return {
            "student_answer": answer,
            "is_correct": None,
            "confidence_score": 0.5,
            "feedback_text": "老师正在审核这道题。",
            "encouragement": "继续努力，你可以的！",
            "next_hint": None,
            "error_type": None,
            "hint_level": 0,
            "attempt_number": 1,
            "routed_to_human": True,
            "grading_source": "pending_human_review",
            "agent_trace": [{"node": "router"}],
        }

    result = await repository.submit_assignment(
        student,
        {
            "assignment_id": ASSIGNMENT_ID,
            "answers": [{"problem_id": PROBLEM_ID, "answer_text": "uncertain:2"}],
        },
        grade_problem,
    )

    executed_sql = "\n".join(call.args[0] for call in connection.execute.await_args_list)
    fetched_sql = "\n".join(call.args[0] for call in connection.fetchval.await_args_list)
    assert "INSERT INTO submission_answers" in executed_sql
    assert "INSERT INTO human_review_queue" in executed_sql
    assert "INSERT INTO grading_results" in fetched_sql
    assert "UPDATE submissions SET status = $3" in fetched_sql
    assert result["status"] == "partial_human_review"
    assert result["summary"]["pending_review"] == 1
    assert "agent_trace" not in result["results"][0]


@pytest.mark.asyncio
async def test_assignment_stats_checks_visibility_and_aggregates_problem_results() -> None:
    connection = AsyncMock()
    connection.fetchrow.side_effect = [
        {
            "id": UUID(ASSIGNMENT_ID),
            "title": "A",
            "due_date": None,
            "class_ids": [UUID(CLASS_ID)],
        },
        {"total_results": 2, "correct_results": 1},
    ]
    connection.fetchval.side_effect = [3, 2]
    connection.fetch.return_value = [
        {
            "id": UUID(PROBLEM_ID),
            "problem_text": "1 + 1 = ___",
            "position": 1,
            "total_attempts": 3,
            "correct_first_try": 1,
            "answered_count": 2,
            "correct_after_hint": 0,
            "still_wrong": 1,
            "pending_review": 0,
            "avg_hint_used": 0.5,
            "error_counts": {"计算错误": 1},
        }
    ]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    stats = await repository.assignment_stats(teacher, ASSIGNMENT_ID)

    assert stats["total_students"] == 3
    assert stats["submitted_count"] == 2
    assert stats["submission_rate"] == 0.667
    assert stats["average_accuracy"] == 0.5
    assert stats["problem_stats"][0]["accuracy_first_try"] == 0.5
    assert stats["problem_stats"][0]["top_error_types"] == [{"error_type": "计算错误", "count": 1, "percentage": 0.5}]
    fetched_sql = "\n".join(call.args[0] for call in connection.fetch.await_args_list)
    assert "assignment_problems" in fetched_sql
    assert "grading_results" in fetched_sql


@pytest.mark.asyncio
async def test_teacher_dashboard_aggregates_visible_classes() -> None:
    connection = AsyncMock()
    connection.fetch.side_effect = [
        [{"id": UUID(CLASS_ID), "name": "Class A"}],
        [{"error_type": "calculation_error", "count": 2}],
        [{"bucket": datetime(2026, 7, 20, tzinfo=UTC).date(), "total_results": 4, "correct_results": 3}],
        [{"knowledge_point": "addition", "affected_student_count": 2, "total_results": 4, "error_rate": 0.5}],
        [
            {
                "student_id": UUID("44444444-4444-4444-8444-444444444444"),
                "student_name": "Student",
                "recent_accuracy": 0.5,
                "weak_points": ["calculation_error"],
                "hint_dependency_rate": 0.75,
                "consecutive_wrong_count": 2,
            }
        ],
    ]
    connection.fetchval.side_effect = [2, 1]
    connection.fetchrow.return_value = {
        "total_submissions": 1,
        "total_results": 4,
        "correct_results": 3,
        "human_review_results": 1,
    }
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    dashboard = await repository.teacher_dashboard(teacher, class_id=None, assignment_id=None, days=30)

    assert dashboard["class_name"] == "Class A"
    assert dashboard["overview"]["total_submissions"] == 1
    assert dashboard["overview"]["average_accuracy"] == 0.75
    assert dashboard["overview"]["submission_rate"] == 0.5
    assert dashboard["overview"]["human_review_rate"] == 0.25
    assert dashboard["error_distribution"] == {"calculation_error": 2}
    assert dashboard["knowledge_point_alerts"] == [
        {
            "knowledge_point": "addition",
            "error_rate": 0.5,
            "alert_level": "high",
            "alert": "超过40%学生在此知识点出错，建议重点讲解",
            "affected_student_count": 2,
        }
    ]
    assert dashboard["students_needing_attention"] == [
        {
            "student_id": "44444444-4444-4444-8444-444444444444",
            "student_name": "Student",
            "recent_accuracy": 0.5,
            "weak_points": ["calculation_error"],
            "hint_dependency_rate": 0.75,
            "consecutive_wrong_count": 2,
        }
    ]
    class_sql = connection.fetch.await_args_list[0].args[0]
    assert "teacher_id = $4::uuid" in class_sql
    alert_sql = connection.fetch.await_args_list[3].args[0]
    assert "student_error_history" in alert_sql
    attention_sql = connection.fetch.await_args_list[4].args[0]
    assert "submission_answers" in attention_sql


@pytest.mark.asyncio
async def test_student_analytics_checks_teacher_visibility_and_aggregates_results() -> None:
    connection = AsyncMock()
    connection.fetchrow.side_effect = [
        {
            "id": UUID("44444444-4444-4444-8444-444444444444"),
            "display_name": "Student",
            "grade_level": 3,
            "class_names": ["Class A"],
            "class_ids": [UUID(CLASS_ID)],
        },
        {
            "total_submissions": 2,
            "total_results": 5,
            "correct_results": 3,
            "wrong_results": 2,
            "total_hints_used": 3,
            "hinted_results": 2,
            "max_hint_reached_count": 1,
        },
    ]
    connection.fetch.side_effect = [
        [{"error_type": "calculation_error", "count": 2}],
        [{"bucket": datetime(2026, 7, 21, tzinfo=UTC).date(), "total_results": 5, "correct_results": 3}],
        [
            {
                "point": "addition",
                "error_count": 2,
                "last_error_at": datetime(2026, 7, 21, 8, tzinfo=UTC),
            }
        ],
    ]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    analytics = await repository.student_analytics(teacher, "44444444-4444-4444-8444-444444444444", days=30)

    assert analytics["student_name"] == "Student"
    assert analytics["overall_accuracy"] == 0.6
    assert analytics["hint_usage"]["total_hints_used"] == 3
    assert analytics["hint_usage"]["hint_dependency_rate"] == 0.4
    assert analytics["error_type_breakdown"] == {"calculation_error": 2}
    assert analytics["weak_knowledge_points"][0]["point"] == "addition"


@pytest.mark.asyncio
async def test_assignment_export_returns_problem_stats_and_student_rows() -> None:
    connection = AsyncMock()
    connection.fetchrow.return_value = {
        "id": UUID(ASSIGNMENT_ID),
        "title": "Assignment",
        "due_date": None,
        "class_ids": [UUID(CLASS_ID)],
    }
    connection.fetch.return_value = [
        {
            "submission_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            "student_id": UUID("44444444-4444-4444-8444-444444444444"),
            "student_name": "Student",
            "status": "graded",
            "submitted_at": datetime(2026, 7, 21, 8, tzinfo=UTC),
            "problem_id": UUID(PROBLEM_ID),
            "position": 1,
            "problem_text": "1 + 1 = ___",
            "answer_text": "3",
            "is_correct": False,
            "error_type": "calculation_error",
            "hint_level": 1,
            "attempt_number": 2,
            "confidence_score": 0.9,
            "routed_to_human": False,
        }
    ]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    repository.assignment_stats = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "problem_stats": [
                {
                    "problem_id": PROBLEM_ID,
                    "sequence": 1,
                    "problem_text": "1 + 1 = ___",
                    "total_attempts": 2,
                    "correct_first_try": 0,
                    "correct_after_hint": 0,
                    "still_wrong": 1,
                    "pending_review": 0,
                    "accuracy_first_try": 0.0,
                    "top_error_types": [{"error_type": "calculation_error", "count": 1, "percentage": 1.0}],
                    "avg_hint_used": 1.0,
                }
            ]
        }
    )
    teacher = PostgresIdentityProblemRepository.user_from_row(user_row())

    exported = await repository.assignment_export(teacher, ASSIGNMENT_ID)

    assert exported["title"] == "Assignment"
    assert exported["problem_stats"][0]["problem_id"] == PROBLEM_ID
    assert exported["student_rows"][0]["student_name"] == "Student"
    assert exported["student_rows"][0]["results"][0]["student_answer"] == "3"
    sql = connection.fetch.await_args.args[0]
    assert "DISTINCT ON (gr.submission_id, gr.problem_id)" in sql
    assert "assignment_problems" in sql


@pytest.mark.asyncio
async def test_list_human_reviews_accepts_class_filter() -> None:
    connection = AsyncMock()
    review_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    submission_id = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    grading_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    connection.fetch.return_value = [
        {
            "review_id": review_id,
            "tenant_id": UUID(TENANT),
            "reason": "low_confidence",
            "status": "pending",
            "created_at": datetime(2026, 7, 22, tzinfo=UTC),
            "reviewer_notes": None,
            "is_training_example": False,
            "grading_result_id": grading_id,
            "submission_id": submission_id,
            "problem_id": UUID(PROBLEM_ID),
            "is_correct": None,
            "confidence_score": 0.5,
            "agent_trace": [],
            "student_answer": "uncertain",
            "student_id": UUID("44444444-4444-4444-8444-444444444444"),
            "student_name": "Student",
            "assignment_title": "Class A Review",
            "problem_text": "1 + 1 = ___",
            "problem_type": "arithmetic",
            "reference_answer": "2",
            "class_names": ["Class A"],
            "class_ids": [UUID(CLASS_ID)],
        }
    ]
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin", class_ids=[]))

    data, pending_count = await repository.list_human_reviews(
        admin,
        status="pending",
        class_id=CLASS_ID,
        page_number=1,
        page_size=20,
    )

    sql, tenant_arg, review_arg, class_arg = connection.fetch.await_args.args
    assert "ac.class_id = $3::uuid" in sql
    assert (tenant_arg, review_arg, class_arg) == (TENANT, None, CLASS_ID)
    assert pending_count == 1
    assert data["items"][0]["review_id"] == str(review_id)
    assert data["items"][0]["assignment_title"] == "Class A Review"


@pytest.mark.asyncio
async def test_update_user_status_toggles_soft_delete_token_and_audit() -> None:
    connection = AsyncMock()
    target_id = UUID("44444444-4444-4444-8444-444444444444")
    connection.fetchrow.return_value = {
        "id": target_id,
        "username": "student",
        "display_name": "Student",
        "role": "student",
        "is_deleted": True,
    }
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin"))

    result = await repository.update_user_status(admin, str(target_id), False)

    assert result["is_active"] is False
    update_sql, tenant_arg, target_arg, deleted_arg = connection.fetchrow.await_args.args
    assert "token_version = token_version + 1" in update_sql
    assert "SET is_deleted = $3" in update_sql
    assert (tenant_arg, target_arg, deleted_arg) == (TENANT, str(target_id), True)
    audit_sql, _tenant, _operator, action, resource_id, detail = connection.execute.await_args.args
    assert "audit_logs" in audit_sql
    assert action == "USER_SUSPENDED"
    assert resource_id == str(target_id)
    assert '"is_active": false' in detail


@pytest.mark.asyncio
async def test_update_user_status_rejects_self_suspend() -> None:
    connection = AsyncMock()
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin"))

    with pytest.raises(AppError) as exc:
        await repository.update_user_status(admin, USER_ID, False)

    assert exc.value.status_code == 409
    connection.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_class_soft_deletes_links_and_audits() -> None:
    connection = AsyncMock()
    connection.fetchrow.return_value = {
        "id": UUID(CLASS_ID),
        "name": "Class A",
        "grade_level": 3,
        "teacher_id": UUID(USER_ID),
        "academic_year": "2026-2027",
    }
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin"))

    result = await repository.delete_class(admin, CLASS_ID)

    assert result == {"class_id": CLASS_ID, "deleted": True}
    update_sql, tenant_arg, class_arg = connection.fetchrow.await_args.args
    assert "UPDATE classes" in update_sql
    assert "is_deleted = true" in update_sql
    assert (tenant_arg, class_arg) == (TENANT, CLASS_ID)
    business_calls = [call.args for call in connection.execute.await_args_list if "set_config" not in call.args[0]]
    link_sql, _tenant, _class_id = business_calls[0]
    assert "UPDATE class_students" in link_sql
    assert "is_active = false" in link_sql
    audit_sql, _tenant, _operator, resource_id, detail = business_calls[1]
    assert "audit_logs" in audit_sql
    assert "'CLASS_DELETED'" in audit_sql
    assert resource_id == CLASS_ID
    assert '"name": "Class A"' in detail


@pytest.mark.asyncio
async def test_delete_class_returns_not_found_for_missing_or_deleted_class() -> None:
    connection = AsyncMock()
    connection.fetchrow.return_value = None
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin"))

    with pytest.raises(AppError) as exc:
        await repository.delete_class(admin, CLASS_ID)

    assert exc.value.status_code == 404
    business_sql = "\n".join(call.args[0] for call in connection.execute.await_args_list)
    assert "UPDATE class_students" not in business_sql
    assert "CLASS_DELETED" not in business_sql


@pytest.mark.asyncio
async def test_admin_stats_overview_reports_real_latency_metrics() -> None:
    connection = AsyncMock()
    connection.fetchrow.side_effect = [
        {"name": "Soaring School", "active_school_year": "2026-2027"},
        {"students": 30, "teachers": 3, "active_students": 12, "active_teachers": 2},
        {"total": 40, "today": 5, "week": 18, "month": 32},
        {"total": 80, "ai_graded": 70, "human_review": 8, "rule_fallback": 2, "correct": 64},
        {"avg_grading_latency_ms": 123.45, "p95_grading_latency_ms": 456.78},
    ]
    connection.fetchval.return_value = 4
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    admin = PostgresIdentityProblemRepository.user_from_row(user_row(role="admin"))

    overview = await repository.admin_stats_overview(admin)

    assert overview["tenant_name"] == "Soaring School"
    assert overview["users"]["total_classes"] == 4
    assert overview["submissions"]["this_week"] == 18
    assert overview["grading"]["human_review_rate"] == 0.1
    assert overview["grading"]["average_accuracy"] == 0.8
    assert overview["performance"] == {"avg_grading_latency_ms": 123.5, "p95_grading_latency_ms": 456.8}
    latency_sql = connection.fetchrow.await_args_list[4].args[0]
    assert "percentile_cont(0.95)" in latency_sql
    assert "latency_ms IS NOT NULL" in latency_sql


@pytest.mark.asyncio
async def test_rag_ingest_job_marks_matching_problems_indexed() -> None:
    connection = AsyncMock()
    job_id = UUID("99999999-9999-4999-8999-999999999999")
    problem_id = UUID("88888888-8888-4888-8888-888888888888")
    connection.fetchval.side_effect = [12, 7]
    connection.fetch.return_value = [
        {
            "id": problem_id,
            "problem_type": "arithmetic",
            "grade_level": 3,
            "difficulty": "medium",
            "problem_text": "1 + 1 = ___",
            "reference_answer": "2",
            "tags": ["加法"],
        }
    ]
    connection.fetchrow.return_value = {"id": job_id, "status": "succeeded"}
    repository = PostgresIdentityProblemRepository(fake_pool(connection), TENANT)
    sysadmin = PostgresIdentityProblemRepository.user_from_row(user_row(role="sysadmin"))
    rag_indexer = FakeRagIndexer()

    result = await repository.create_rag_ingest_job(
        sysadmin,
        {"source": "problems_table", "grade_levels": [3], "batch_size": 100, "force_reingest": False},
        rag_indexer,
    )

    assert result["job_id"] == str(job_id)
    assert result["status"] == "succeeded"
    assert result["matched_problem_count"] == 12
    assert result["ingested_count"] == 7
    assert connection.fetch.await_args.args[3] is False
    assert connection.fetch.await_args.args[4] == 100
    assert rag_indexer.calls[0][0] == TENANT
    assert rag_indexer.calls[0][1][0]["problem_id"] == str(problem_id)
    update_sql = connection.fetchval.await_args_list[1].args[0]
    assert "embedding_status = 'done'" in update_sql
    assert "id = ANY($2::uuid[])" in update_sql
    assert connection.fetchval.await_args_list[1].args[2] == [str(problem_id)]
    sql = connection.fetchrow.await_args.args[0]
    result_json = connection.fetchrow.await_args.args[3]
    assert "'rag_ingest', 'succeeded'" in sql
    assert "error_message" not in sql
    assert '"qdrant_status": "qdrant_indexed"' in result_json
