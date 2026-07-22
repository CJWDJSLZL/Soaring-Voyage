"""Opt-in PostgreSQL integration coverage.

These tests require a migrated database, a pre-existing TEST_DEFAULT_TENANT_ID,
and TEST_DATABASE_URL credentials for the non-owner soaring_voyage_app runtime role.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
from app.config import Settings
from app.core.security import hash_password
from app.db.session import tenant_conn, tenant_context
from app.domain.postgres import NIL_SYSTEM_USER_ID, PostgresIdentityProblemRepository
from app.main import create_app
from fastapi.testclient import TestClient

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
TENANT_ID = os.getenv("TEST_DEFAULT_TENANT_ID")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not TENANT_ID,
    reason="TEST_DATABASE_URL and TEST_DEFAULT_TENANT_ID are required",
)


@pytest.mark.asyncio
async def test_runtime_role_rls_identity_token_version_and_problem_persistence() -> None:
    assert DATABASE_URL is not None
    assert TENANT_ID is not None
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2)
    repository = PostgresIdentityProblemRepository(pool, TENANT_ID)
    user_id = str(uuid4())
    student_id = str(uuid4())
    class_id = str(uuid4())
    username = f"integration-{uuid4().hex}"
    student_username = f"integration-student-{uuid4().hex}"
    problem_ids: list[str] = []
    assignment_ids: list[str] = []
    try:
        async with pool.acquire() as connection:
            role = await connection.fetchrow(
                "SELECT r.rolname, r.rolsuper, r.rolbypassrls FROM pg_roles r WHERE r.rolname = current_user"
            )
            assert role["rolname"] == "soaring_voyage_app"
            assert role["rolsuper"] is False
            assert role["rolbypassrls"] is False

        with tenant_context(TENANT_ID, "worker"):
            async with tenant_conn(pool, user_id=NIL_SYSTEM_USER_ID) as connection:
                await connection.execute(
                    """
                    INSERT INTO users (id, tenant_id, role, username, display_name, password_hash)
                    VALUES ($1, $2, 'teacher', $3, 'Integration Teacher', $4)
                    """,
                    user_id,
                    TENANT_ID,
                    username,
                    hash_password("Integration@123").decode("utf-8"),
                )
                await connection.execute(
                    """
                    INSERT INTO users (id, tenant_id, role, username, display_name, password_hash, grade_level)
                    VALUES ($1, $2, 'student', $3, 'Integration Student', $4, 3)
                    """,
                    student_id,
                    TENANT_ID,
                    student_username,
                    hash_password("Integration@123").decode("utf-8"),
                )
                await connection.execute(
                    """
                    INSERT INTO classes (id, tenant_id, grade_level, name, teacher_id, academic_year)
                    VALUES ($1, $2, 3, 'Integration Class', $3, '2026-2027')
                    """,
                    class_id,
                    TENANT_ID,
                    user_id,
                )
                await connection.execute(
                    """
                    INSERT INTO class_students (tenant_id, class_id, student_id)
                    VALUES ($1, $2, $3)
                    """,
                    TENANT_ID,
                    class_id,
                    student_id,
                )

        user = await repository.identity_by_username(username)
        assert user is not None
        assert user.user_id == user_id
        assert user.token_version == 0

        await repository.increment_token_version(user)
        refreshed = await repository.identity_by_id(user_id, TENANT_ID, "teacher")
        assert refreshed is not None
        assert refreshed.token_version == 1

        problem_id = await repository.create_catalog_problem(
            refreshed,
            {
                "problem_type": "arithmetic",
                "grade_level": 3,
                "difficulty": "easy",
                "curriculum_version": "人教版",
                "problem_text": "integration 1 + 1",
                "reference_answer": "2",
                "solution_steps": ["add"],
                "common_errors": [],
                "tags": ["integration"],
            },
        )
        problem_ids.append(problem_id)
        listed = await repository.list_catalog_problems(
            refreshed,
            grade_level=3,
            problem_type="arithmetic",
            difficulty=None,
            keyword="integration 1 + 1",
            tags=["integration"],
            source="school",
            page_number=1,
            page_size=20,
        )
        assert problem_id in {item["problem_id"] for item in listed["items"]}

        configured = Settings(
            app_env="test",
            persistence_backend="postgres",
            database_url=DATABASE_URL,
            default_tenant_id=TENANT_ID,
            jwt_secret="postgres-integration-secret-at-least-32-characters",
        )
        test_app = create_app(configured)
        with TestClient(test_app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": username, "password": "Integration@123"},
            )
            assert login.status_code == 200
            authorization = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
            created = client.post(
                "/api/v1/problems/",
                headers=authorization,
                json={
                    "problem_type": "arithmetic",
                    "grade_level": 3,
                    "difficulty": "easy",
                    "curriculum_version": "人教版",
                    "problem_text": "integration API 2 + 2",
                    "reference_answer": "4",
                    "solution_steps": ["add"],
                    "common_errors": [],
                    "tags": ["integration-api"],
                },
            )
            assert created.status_code == 201
            api_problem_id = created.json()["data"]["problem_id"]
            problem_ids.append(api_problem_id)
            catalog = client.get(
                "/api/v1/problems/",
                headers=authorization,
                params={"keyword": "integration API 2 + 2"},
            )
            assert catalog.status_code == 200
            assert api_problem_id in {item["problem_id"] for item in catalog.json()["data"]["items"]}
            assignment = client.post(
                "/api/v1/assignments/",
                headers=authorization,
                json={
                    "title": "Integration Assignment",
                    "class_ids": [class_id],
                    "due_date": None,
                    "problem_ids": [api_problem_id],
                },
            )
            assert assignment.status_code == 201
            assignment_id = assignment.json()["data"]["assignment_id"]
            assignment_ids.append(assignment_id)
            assignments = client.get("/api/v1/assignments/", headers=authorization)
            assert assignments.status_code == 200
            assert assignment_id in {item["assignment_id"] for item in assignments.json()["data"]["items"]}
            detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=authorization)
            assert detail.status_code == 200
            assert detail.json()["data"]["problems"][0]["problem_id"] == api_problem_id
            patched = client.patch(
                f"/api/v1/assignments/{assignment_id}",
                headers=authorization,
                json={"title": "Integration Assignment Updated"},
            )
            assert patched.status_code == 200
            assert patched.json()["data"]["title"] == "Integration Assignment Updated"

            student_login = client.post(
                "/api/v1/auth/login",
                json={"username": student_username, "password": "Integration@123"},
            )
            assert student_login.status_code == 200
            student_auth = {"Authorization": f"Bearer {student_login.json()['data']['access_token']}"}
            student_detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=student_auth)
            assert student_detail.status_code == 200
            assert student_detail.json()["data"]["my_submission"] is None
            submission = client.post(
                "/api/v1/submissions/",
                headers=student_auth,
                json={
                    "assignment_id": assignment_id,
                    "answers": [{"problem_id": api_problem_id, "answer_text": "uncertain:4"}],
                },
            )
            assert submission.status_code == 201
            submission_data = submission.json()["data"]
            assert submission_data["status"] == "partial_human_review"
            assert submission_data["summary"] == {
                "total": 1,
                "correct": 0,
                "wrong": 0,
                "pending_review": 1,
                "accuracy": 0.0,
            }
            assert submission_data["results"][0]["is_correct"] is None
            assert "agent_trace" not in submission_data["results"][0]

            reviews = client.get("/api/v1/teacher/human-review-queue", headers=authorization)
            assert reviews.status_code == 200
            assert reviews.headers["X-Pending-Review-Count"] == "1"
            review_id = reviews.json()["data"]["items"][0]["review_id"]
            review_detail = client.get(f"/api/v1/teacher/human-review/{review_id}", headers=authorization)
            assert review_detail.status_code == 200
            assert review_detail.json()["data"]["submission_id"] == submission_data["submission_id"]

            duplicate = client.post(
                "/api/v1/submissions/",
                headers=student_auth,
                json={"assignment_id": assignment_id, "answers": [{"problem_id": api_problem_id, "answer_text": "4"}]},
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == 4005

            refreshed_detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=student_auth)
            assert refreshed_detail.status_code == 200
            assert refreshed_detail.json()["data"]["my_submission"]["submission_id"] == submission_data["submission_id"]
            submission_detail = client.get(
                f"/api/v1/submissions/{submission_data['submission_id']}",
                headers=student_auth,
            )
            assert submission_detail.status_code == 200
            assert submission_detail.json()["data"]["results"][0]["student_answer"] == "4"
            listed_submissions = client.get("/api/v1/submissions/", headers=student_auth)
            assert listed_submissions.status_code == 200
            assert submission_data["submission_id"] in {
                item["submission_id"] for item in listed_submissions.json()["data"]["items"]
            }
            hint = client.post(
                f"/api/v1/submissions/{submission_data['submission_id']}/hint",
                headers=student_auth,
                json={"problem_id": api_problem_id, "new_answer": "4"},
            )
            assert hint.status_code == 200
            assert hint.json()["data"]["is_correct"] is True
            hinted_detail = client.get(
                f"/api/v1/submissions/{submission_data['submission_id']}",
                headers=student_auth,
            )
            assert hinted_detail.status_code == 200
            assert hinted_detail.json()["data"]["status"] == "graded"

            ticket = client.post(
                "/api/v1/auth/sse-ticket",
                headers=student_auth,
                json={"submission_id": submission_data["submission_id"]},
            )
            assert ticket.status_code == 200
            events = client.get(
                f"/api/v1/submissions/{submission_data['submission_id']}/events",
                params={"sse_ticket": ticket.json()["data"]["ticket"], "follow": "false"},
            )
            assert events.status_code == 200
            assert "event: grading_update" in events.text
            assert submission_data["submission_id"] in events.text

        other_tenant = str(uuid4())
        with tenant_context(other_tenant, "worker"):
            async with tenant_conn(pool, user_id=NIL_SYSTEM_USER_ID) as connection:
                assert await connection.fetchval("SELECT count(*) FROM users WHERE id = $1", user_id) == 0
                assert await connection.fetchval("SELECT count(*) FROM problems WHERE id = $1", problem_id) == 0
    finally:
        with tenant_context(TENANT_ID, "worker"):
            async with tenant_conn(pool, user_id=NIL_SYSTEM_USER_ID) as connection:
                for persisted_assignment_id in assignment_ids:
                    await connection.execute(
                        "DELETE FROM assignments WHERE tenant_id = $1 AND id = $2",
                        TENANT_ID,
                        persisted_assignment_id,
                    )
                for persisted_problem_id in problem_ids:
                    await connection.execute(
                        "DELETE FROM problems WHERE tenant_id = $1 AND id = $2",
                        TENANT_ID,
                        persisted_problem_id,
                    )
                await connection.execute(
                    "DELETE FROM class_students WHERE tenant_id = $1 AND class_id = $2", TENANT_ID, class_id
                )
                await connection.execute("DELETE FROM classes WHERE tenant_id = $1 AND id = $2", TENANT_ID, class_id)
                await connection.execute("DELETE FROM users WHERE tenant_id = $1 AND id = $2", TENANT_ID, student_id)
                await connection.execute("DELETE FROM users WHERE tenant_id = $1 AND id = $2", TENANT_ID, user_id)
        await pool.close()
