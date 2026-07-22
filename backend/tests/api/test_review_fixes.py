from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from app.api.routes import sse_event_stream
from app.domain.models import Ticket
from app.main import app
from fastapi.testclient import TestClient
from tests.api.test_core_api import auth, login, make_problem_and_assignment


def test_non_development_settings_fail_fast_without_strong_secret() -> None:
    env = {**os.environ, "APP_ENV": "production", "SECRET_KEY": "weak"}
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "SECRET_KEY must be a strong value" in result.stderr


def test_non_development_settings_refuse_unwired_adapters() -> None:
    env = {**os.environ, "APP_ENV": "production", "SECRET_KEY": "x" * 40}
    result = subprocess.run(
        [sys.executable, "-c", "import app.config"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "Production startup refused" in result.stderr


def test_health_cors_trusted_host_and_token_detail() -> None:
    app.state.store.reset()
    client = TestClient(app)
    health = client.get("/health")
    body = health.json()
    assert body["status"] == "degraded"
    assert body["version"] == "1.0.0"
    assert body["uptime_seconds"] >= 0
    assert body["grading"] == {"active_requests": 0, "pending_hitl_count": 0}
    assert body["services"]["database"]["status"] == "not-wired"
    assert body["services"]["qdrant"]["backend"] == "local-metadata-index"
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400

    cors = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://localhost", "Access-Control-Request-Method": "POST"},
    )
    assert cors.headers["access-control-allow-origin"] == "http://localhost"
    invalid = client.get("/api/v1/submissions/", headers={"Authorization": "Bearer invalid"})
    assert invalid.status_code == 401
    assert "detail" not in invalid.json()


def test_health_reports_pending_human_review_count() -> None:
    app.state.store.reset()
    client = TestClient(app)
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "uncertain:0"}]},
    )
    assert submitted.status_code == 201, submitted.text

    health = client.get("/health")

    assert health.status_code == 200
    assert health.json()["grading"]["pending_hitl_count"] == 1


def test_assignment_patch_validates_tenant_due_count_and_classes() -> None:
    app.state.store.reset()
    client = TestClient(app)
    teacher = login(client, "teacher")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)

    unknown_class = client.patch(
        f"/api/v1/assignments/{assignment_id}", headers=auth(teacher), json={"class_ids": ["unknown"]}
    )
    assert unknown_class.status_code == 404
    expired = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"due_date": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
    )
    assert expired.status_code == 422

    app.state.store.problems["foreign"] = {**app.state.store.problems[problem_id], "tenant_id": "other"}
    foreign = client.patch(
        f"/api/v1/assignments/{assignment_id}", headers=auth(teacher), json={"add_problem_ids": ["foreign"]}
    )
    assert foreign.status_code == 404

    assignment = app.state.store.assignments[assignment_id]
    for index in range(49):
        pid = f"extra-{index}"
        app.state.store.problems[pid] = {**app.state.store.problems[problem_id], "problem_id": pid}
        assignment["problem_ids"].append(pid)
    overflow_id = "overflow"
    app.state.store.problems[overflow_id] = {**app.state.store.problems[problem_id], "problem_id": overflow_id}
    overflow = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"add_problem_ids": [overflow_id]},
    )
    assert overflow.status_code == 422


def test_hint_regrade_supersedes_review_recomputes_summary_and_events() -> None:
    app.state.store.reset()
    client = TestClient(app)
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "uncertain:0"}]},
    ).json()["data"]
    submission_id = submitted["submission_id"]

    corrected = client.post(
        f"/api/v1/submissions/{submission_id}/hint",
        headers=auth(student),
        json={"problem_id": problem_id, "new_answer": "372"},
    )
    assert corrected.status_code == 200
    stored = app.state.store.submissions[submission_id]
    assert stored["status"] == "graded"
    assert stored["summary"] == {"total": 1, "correct": 1, "wrong": 0, "pending_review": 0, "accuracy": 1.0}
    old_review = list(app.state.store.reviews.values())[0]
    assert old_review["status"] == "reviewed"
    assert old_review["resolution"] == "superseded"
    assert app.state.store.events[submission_id][-1]["is_correct"] is True


def test_sse_heartbeat_renderer_and_expired_ticket_cleanup() -> None:
    chunks = list(sse_event_stream([{"status": "graded"}], heartbeat_count=2))
    assert "event: grading_update" in chunks[0]
    assert chunks[1:] == [": heartbeat\n\n", ": heartbeat\n\n"]

    resumed = list(sse_event_stream([{"status": "first"}, {"status": "second"}], last_event_id=1, heartbeat_count=0))
    assert len(resumed) == 1
    assert "id: 2" in resumed[0]
    assert '"status": "second"' in resumed[0]

    app.state.store.reset()
    app.state.store.tickets["expired"] = Ticket(
        "u", "tenant-demo", "s", "student", datetime.now(UTC) - timedelta(seconds=1)
    )
    assert app.state.store.purge_expired_tickets() == 1
    assert not app.state.store.tickets


def test_submission_events_resume_after_last_event_id() -> None:
    app.state.store.reset()
    client = TestClient(app)
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    submitted = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "uncertain:0"}]},
    )
    assert submitted.status_code == 201, submitted.text
    submission_id = submitted.json()["data"]["submission_id"]
    app.state.store.events[submission_id].append({"submission_id": submission_id, "status": "reviewed"})
    ticket = client.post(
        "/api/v1/auth/sse-ticket",
        headers=auth(student),
        json={"submission_id": submission_id},
    )
    assert ticket.status_code == 200

    events = client.get(
        f"/api/v1/submissions/{submission_id}/events",
        headers={"Last-Event-ID": "1"},
        params={"sse_ticket": ticket.json()["data"]["ticket"], "follow": "false"},
    )

    assert events.status_code == 200
    assert "id: 1" not in events.text
    assert "id: 2" in events.text
    assert '"status": "reviewed"' in events.text


def test_logout_revokes_existing_token() -> None:
    app.state.store.reset()
    client = TestClient(app)
    token = login(client, "student")
    assert client.post("/api/v1/auth/logout", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/assignments/", headers=auth(token)).status_code == 401


def test_submission_requires_exactly_one_answer_per_assignment_problem() -> None:
    app.state.store.reset()
    client = TestClient(app)
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    second_id = "second-problem"
    app.state.store.problems[second_id] = {
        **app.state.store.problems[problem_id],
        "problem_id": second_id,
    }
    app.state.store.assignments[assignment_id]["problem_ids"].append(second_id)

    partial = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "372"}]},
    )
    assert partial.status_code == 422
    duplicate = client.post(
        "/api/v1/submissions/",
        headers=auth(student),
        json={
            "assignment_id": assignment_id,
            "answers": [
                {"problem_id": problem_id, "answer_text": "372"},
                {"problem_id": problem_id, "answer_text": "372"},
            ],
        },
    )
    assert duplicate.status_code == 422


def test_assignment_patch_cannot_remove_every_problem() -> None:
    app.state.store.reset()
    client = TestClient(app)
    teacher = login(client, "teacher")
    problem_id, assignment_id = make_problem_and_assignment(client, teacher)
    response = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"remove_problem_ids": [problem_id]},
    )
    assert response.status_code == 422

    duplicate = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"add_problem_ids": [problem_id, problem_id]},
    )
    assert duplicate.status_code == 422
    remove_duplicate = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"remove_problem_ids": [problem_id, problem_id]},
    )
    assert remove_duplicate.status_code == 422
    overlap = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"add_problem_ids": [problem_id], "remove_problem_ids": [problem_id]},
    )
    assert overlap.status_code == 422


def test_admin_soft_deletes_class_and_removes_it_from_admin_workflows() -> None:
    app.state.store.reset()
    client = TestClient(app)
    admin = login(client, "admin")
    teacher = login(client, "teacher")
    initial_classes = client.get("/api/v1/admin/stats/overview", headers=auth(admin)).json()["data"]["users"][
        "total_classes"
    ]

    created = client.post(
        "/api/v1/admin/classes/",
        headers=auth(admin),
        json={
            "name": "待删除班级",
            "grade_level": 3,
            "teacher_id": "user-teacher",
            "academic_year": "2026-2027",
        },
    )
    assert created.status_code == 201, created.text
    class_id = created.json()["data"]["class_id"]
    assert class_id in app.state.store.users["teacher"].class_ids

    denied = client.delete(f"/api/v1/admin/classes/{class_id}", headers=auth(teacher))
    assert denied.status_code == 403
    deleted = client.delete(f"/api/v1/admin/classes/{class_id}", headers=auth(admin))

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"] == {"class_id": class_id, "deleted": True}
    assert app.state.store.classes[class_id]["is_deleted"] is True
    assert class_id not in app.state.store.users["teacher"].class_ids
    assert client.delete(f"/api/v1/admin/classes/{class_id}", headers=auth(admin)).status_code == 404

    problem = client.post(
        "/api/v1/problems/",
        headers=auth(admin),
        json={
            "problem_text": "1 + 2 = ___",
            "problem_type": "arithmetic",
            "reference_answer": "3",
            "grade_level": 3,
            "difficulty": "easy",
            "curriculum_version": "人教版",
        },
    )
    assert problem.status_code == 201, problem.text
    assignment = client.post(
        "/api/v1/assignments/",
        headers=auth(admin),
        json={
            "title": "不能布置到已删除班级",
            "class_ids": [class_id],
            "due_date": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "problem_ids": [problem.json()["data"]["problem_id"]],
        },
    )
    assert assignment.status_code == 404

    imported = client.post(
        "/api/v1/admin/students/bulk-create",
        headers=auth(admin),
        files={
            "file": (
                "students.csv",
                "姓名,用户名,初始密码,年级,班级名称\n测试学生,deleted_class_student,Import123,3,待删除班级\n".encode(),
                "text/csv",
            )
        },
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["data"]["created"] == 0
    assert imported.json()["data"]["failed"] == 1
    assert imported.json()["data"]["failed_rows"][0]["reason"] == "class does not exist"
    final_classes = client.get("/api/v1/admin/stats/overview", headers=auth(admin)).json()["data"]["users"][
        "total_classes"
    ]
    assert final_classes == initial_classes


def test_followed_sse_stream_observes_events_added_after_connection() -> None:
    events: list[dict] = []
    stream = sse_event_stream(events, heartbeat_count=None, follow=True, poll_seconds=0)
    assert next(stream) == ": heartbeat\n\n"
    events.append({"status": "reviewed"})
    assert "event: grading_update" in next(stream)
