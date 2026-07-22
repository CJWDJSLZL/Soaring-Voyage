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

    app.state.store.reset()
    app.state.store.tickets["expired"] = Ticket(
        "u", "tenant-demo", "s", "student", datetime.now(UTC) - timedelta(seconds=1)
    )
    assert app.state.store.purge_expired_tickets() == 1
    assert not app.state.store.tickets


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


def test_followed_sse_stream_observes_events_added_after_connection() -> None:
    events: list[dict] = []
    stream = sse_event_stream(events, heartbeat_count=None, follow=True, poll_seconds=0)
    assert next(stream) == ": heartbeat\n\n"
    events.append({"status": "reviewed"})
    assert "event: grading_update" in next(stream)
