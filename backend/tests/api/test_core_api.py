from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.config import settings
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_state():
    app.state.store.reset()


@pytest.fixture
def client():
    return TestClient(app)


def login(client: TestClient, username: str, password: str = "Test@1234") -> str:
    response = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0 and body["trace_id"].startswith("req-")
    return body["data"]["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_problem_and_assignment(client: TestClient, teacher_token: str, *, answer: str = "372"):
    problem = client.post(
        "/api/v1/problems/",
        headers=auth(teacher_token),
        json={
            "problem_text": "325 + 47 = ___",
            "problem_type": "arithmetic",
            "reference_answer": answer,
            "grade_level": 3,
            "difficulty": "medium",
            "curriculum_version": "人教版",
            "solution_steps": ["个位相加并处理进位", "再计算十位和百位"],
            "tags": ["加法", "进位"],
        },
    )
    assert problem.status_code == 201, problem.text
    problem_id = problem.json()["data"]["problem_id"]
    assignment = client.post(
        "/api/v1/assignments/",
        headers=auth(teacher_token),
        json={
            "title": "第三单元练习",
            "class_ids": ["class-3a"],
            "due_date": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "problem_ids": [problem_id],
        },
    )
    assert assignment.status_code == 201, assignment.text
    return problem_id, assignment.json()["data"]["assignment_id"]


def test_login_jwt_claims_and_uniform_validation_error(client: TestClient):
    token = login(client, "student")
    claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    assert claims["role"] == "student"
    assert claims["tenant_id"] == "tenant-demo"
    assert 86390 <= claims["exp"] - claims["iat"] <= 86400

    invalid = client.post("/api/v1/auth/login", json={"username": "has space", "password": "123"})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == 4022
    assert invalid.json()["trace_id"].startswith("req-")


def test_login_five_failures_lock_account(client: TestClient):
    for _ in range(4):
        response = client.post("/api/v1/auth/login", json={"username": "student", "password": "wrongxx"})
        assert response.status_code == 401
    fifth = client.post("/api/v1/auth/login", json={"username": "student", "password": "wrongxx"})
    assert fifth.status_code == 401
    assert fifth.json()["locked_until"]
    locked = client.post("/api/v1/auth/login", json={"username": "student", "password": "Test@1234"})
    assert locked.status_code == 401
    assert "锁定" in locked.json()["message"]


def test_rbac_problem_assignment_and_student_answer_isolation(client: TestClient):
    student = login(client, "student")
    teacher = login(client, "teacher")

    denied = client.post("/api/v1/problems/", headers=auth(student), json={})
    assert denied.status_code == 403 and denied.json()["code"] == 4003

    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=auth(student))
    assert detail.status_code == 200
    serialized = detail.text.lower()
    assert "reference_answer" not in serialized
    assert "solution_steps" not in serialized

    assert detail.json()["data"]["problems"][0]["problem_id"] == problem_id


def test_teacher_bulk_import_problems_from_csv(client: TestClient):
    teacher = login(client, "teacher")
    student = login(client, "student")
    denied = client.post(
        "/api/v1/problems/bulk-import", headers=auth(student), files={"file": ("p.csv", b"", "text/csv")}
    )
    assert denied.status_code == 403
    csv_body = (
        "problem_text,problem_type,reference_answer,grade_level,difficulty,solution_steps,tags\n"
        "1 + 1 = ___,arithmetic,2,1,easy,先算个位;写出结果,加法;一年级\n"
        "下面哪个等于 4,multiple_choice,B,1,easy,逐个算选项,选择题\n"
    ).encode()

    imported = client.post(
        "/api/v1/problems/bulk-import",
        headers=auth(teacher),
        files={"file": ("problems.csv", csv_body, "text/csv")},
        data={"curriculum_version": "renjiao"},
    )

    assert imported.status_code == 202, imported.text
    data = imported.json()["data"]
    assert data["status"] == "succeeded"
    assert data["success"] == 2
    assert data["failed"] == 0
    listed = client.get("/api/v1/problems/?grade_level=1", headers=auth(teacher))
    assert listed.status_code == 200
    assert data["problem_ids"][0] in {item["problem_id"] for item in listed.json()["data"]["items"]}
    job = client.get(f"/api/v1/ops/jobs/{data['import_job_id']}", headers=auth(teacher))
    assert job.status_code == 403


def test_core_submission_hint_and_duplicate_loop(client: TestClient):
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)

    response = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "362"}]},
    )
    assert response.status_code == 201, response.text
    result = response.json()["data"]
    submission_id = result["submission_id"]
    assert result["status"] == "graded"
    assert result["results"][0]["is_correct"] is False
    assert "reference_answer" not in response.text
    assert all("agent_trace" not in item for item in result["results"])

    duplicate = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "372"}]},
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == 4005

    hint = client.post(
        f"/api/v1/submissions/{submission_id}/hint",
        headers=auth(student),
        json={"problem_id": problem_id, "new_answer": "372"},
    )
    assert hint.status_code == 200
    assert hint.json()["data"]["is_correct"] is True
    assert hint.json()["data"]["hint_level"] == 1
    denied_stats = client.get(f"/api/v1/assignments/{assignment_id}/stats", headers=auth(student))
    assert denied_stats.status_code == 403
    stats = client.get(f"/api/v1/assignments/{assignment_id}/stats", headers=auth(teacher))
    assert stats.status_code == 200
    stats_data = stats.json()["data"]
    assert stats_data["total_students"] == 1
    assert stats_data["submitted_count"] == 1
    assert stats_data["average_accuracy"] == 1.0
    assert stats_data["problem_stats"][0]["problem_id"] == problem_id
    assert stats_data["problem_stats"][0]["correct_first_try"] == 0
    assert stats_data["problem_stats"][0]["correct_after_hint"] == 1

    other_student = login(client, "student2")
    hidden = client.get(f"/api/v1/submissions/{submission_id}", headers=auth(other_student))
    assert hidden.status_code == 404


def test_teacher_dashboard_and_student_analytics(client: TestClient):
    teacher = login(client, "teacher")
    student = login(client, "student")
    admin = login(client, "admin")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "371"}]},
    )
    assert submitted.status_code == 201, submitted.text

    denied = client.get("/api/v1/teacher/dashboard", headers=auth(student))
    assert denied.status_code == 403

    dashboard = client.get(
        f"/api/v1/teacher/dashboard?class_id=class-3a&assignment_id={assignment_id}&days=30",
        headers=auth(teacher),
    )
    assert dashboard.status_code == 200, dashboard.text
    data = dashboard.json()["data"]
    assert data["overview"]["total_submissions"] == 1
    assert data["overview"]["average_accuracy"] == 0.0
    assert data["error_distribution"]
    assert data["students_needing_attention"][0]["student_id"] == "user-student"

    analytics = client.get("/api/v1/teacher/students/user-student/analytics?days=30", headers=auth(teacher))
    assert analytics.status_code == 200, analytics.text
    student_data = analytics.json()["data"]
    assert student_data["total_submissions"] == 1
    assert student_data["total_problems_answered"] == 1
    assert student_data["overall_accuracy"] == 0.0
    assert student_data["error_type_breakdown"]

    forbidden = client.get("/api/v1/teacher/students/user-student2/analytics", headers=auth(teacher))
    assert forbidden.status_code == 403
    admin_view = client.get("/api/v1/teacher/students/user-student/analytics", headers=auth(admin))
    assert admin_view.status_code == 200


def test_teacher_exports_assignment_excel_report(client: TestClient):
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "371"}]},
    )
    assert submitted.status_code == 201, submitted.text

    denied = client.get(f"/api/v1/teacher/export/assignment/{assignment_id}", headers=auth(student))
    assert denied.status_code == 403

    exported = client.get(f"/api/v1/teacher/export/assignment/{assignment_id}?format=excel", headers=auth(teacher))

    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "assignment_report_" in exported.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(exported.content)) as workbook:
        assert "xl/worksheets/sheet1.xml" in workbook.namelist()
        assert "xl/worksheets/sheet2.xml" in workbook.namelist()
        sheet1 = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
        sheet2 = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
    assert problem_id in sheet1
    assert "user-student" in sheet2


def test_hitl_review_and_one_time_sse_ticket(client: TestClient):
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "uncertain:362"}]},
    )
    data = submitted.json()["data"]
    assert data["status"] == "partial_human_review"
    submission_id = data["submission_id"]

    queue = client.get("/api/v1/teacher/human-review-queue", headers=auth(teacher))
    assert queue.status_code == 200
    assert queue.headers["X-Pending-Review-Count"] == "1"
    review = queue.json()["data"]["items"][0]
    assert review["reference_answer"] == "372"

    ticket_response = client.post(
        "/api/v1/auth/sse-ticket", headers=auth(student), json={"submission_id": submission_id}
    )
    assert ticket_response.status_code == 200
    ticket = ticket_response.json()["data"]["ticket"]
    with client.stream("GET", f"/api/v1/submissions/{submission_id}/events?sse_ticket={ticket}&follow=false") as events:
        assert events.status_code == 200
        assert "event: grading_update" in "".join(events.iter_text())
    reused = client.get(f"/api/v1/submissions/{submission_id}/events?sse_ticket={ticket}")
    assert reused.status_code == 401

    reviewed = client.post(
        f"/api/v1/teacher/human-review/{review['review_id']}",
        headers=auth(teacher),
        json={
            "override_correct": False,
            "override_error_type": "进位错误",
            "override_feedback": "请再检查进位步骤。",
            "reviewer_notes": "人工确认",
            "is_training_example": True,
        },
    )
    assert reviewed.status_code == 200
    final = client.get(f"/api/v1/submissions/{submission_id}", headers=auth(student)).json()["data"]
    assert final["status"] == "reviewed"
    assert final["results"][0]["grading_source"] == "human_override"
    assert final["summary"]["wrong"] == 1
    assert final["summary"]["accuracy"] == 0
    assert "reference_answer" not in str(final)


def test_hint_level_increments_and_stops_after_three(client: TestClient):
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "0"}]},
    )
    submission_id = submitted.json()["data"]["submission_id"]
    for expected_level in (1, 2, 3):
        response = client.post(
            f"/api/v1/submissions/{submission_id}/hint",
            headers=auth(student),
            json={"problem_id": problem_id, "new_answer": "0"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["hint_level"] == expected_level
        assert response.json()["data"]["remaining_hints"] == 3 - expected_level
    exhausted = client.post(
        f"/api/v1/submissions/{submission_id}/hint",
        headers=auth(student),
        json={"problem_id": problem_id, "new_answer": "0"},
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["code"] == 4007


def test_admin_classes_stats_password_reset_and_ops_jobs(client: TestClient):
    admin = login(client, "admin")
    sysadmin = login(client, "sysadmin")
    student = login(client, "student")

    denied = client.get("/api/v1/admin/stats/overview", headers=auth(student))
    assert denied.status_code == 403

    created = client.post(
        "/api/v1/admin/classes/",
        headers=auth(admin),
        json={
            "name": "三年级C班",
            "grade_level": 3,
            "teacher_id": "user-teacher",
            "academic_year": "2026-2027",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["data"]["name"] == "三年级C班"

    reset = client.post(
        "/api/v1/admin/users/user-student/reset-password",
        headers=auth(admin),
        json={"new_password": "TempPass2026"},
    )
    assert reset.status_code == 200
    assert reset.json()["data"]["force_change_on_next_login"] is True
    assert (
        client.post("/api/v1/auth/login", json={"username": "student", "password": "TempPass2026"}).status_code == 200
    )

    stats = client.get("/api/v1/admin/stats/overview", headers=auth(admin))
    assert stats.status_code == 200
    assert stats.json()["data"]["users"]["total_classes"] >= 3

    harness = client.post(
        "/api/v1/ops/harness/run",
        headers=auth(sysadmin),
        json={"use_mock": True, "sample_rate": 1.0, "dataset": "all", "grade_levels": [1, 2, 3]},
    )
    assert harness.status_code == 202
    run_id = harness.json()["data"]["run_id"]
    harness_detail = client.get(f"/api/v1/ops/harness/runs/{run_id}", headers=auth(sysadmin))
    assert harness_detail.status_code == 200
    assert harness_detail.json()["data"]["status"] == "completed"
    assert harness_detail.json()["data"]["accuracy"] >= 0.94

    rag = client.post(
        "/api/v1/ops/rag/ingest",
        headers=auth(sysadmin),
        json={"source": "problems_table", "grade_levels": [3], "batch_size": 100, "force_reingest": False},
    )
    assert rag.status_code == 202
    job_id = rag.json()["data"]["job_id"]
    job = client.get(f"/api/v1/ops/jobs/{job_id}", headers=auth(sysadmin))
    assert job.status_code == 200
    assert job.json()["data"]["result"]["qdrant_status"] == "not_wired"


def test_admin_bulk_create_students_from_csv(client: TestClient):
    admin = login(client, "admin")
    teacher = login(client, "teacher")
    _problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    class_name = app.state.store.classes["class-3a"]["class_name"]
    csv_body = (
        "姓名,用户名,初始密码,年级,班级名称\n"
        f"新同学,new_student,Import123,3,{class_name}\n"
        f"重复学生,student,Import123,3,{class_name}\n"
        "无班级,missing_class,Import123,3,不存在班级\n"
    ).encode()

    imported = client.post(
        "/api/v1/admin/students/bulk-create",
        headers=auth(admin),
        files={"file": ("students.csv", csv_body, "text/csv")},
    )

    assert imported.status_code == 200, imported.text
    data = imported.json()["data"]
    assert data["created"] == 1
    assert data["skipped"] == 1
    assert data["failed"] == 1
    assert data["skipped_reasons"][0]["username"] == "student"
    assert data["failed_rows"][0]["username"] == "missing_class"

    token = login(client, "new_student", "Import123")
    detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=auth(token))
    assert detail.status_code == 200
    assert detail.json()["data"]["assignment_id"] == assignment_id
