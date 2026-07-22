from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_ENV", "test")

from app.main import app  # noqa: E402


def print_step(title: str, payload: dict[str, Any] | list[Any] | None = None) -> None:
    print(f"\n== {title} ==")
    if payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def require_ok(response, expected_status: int = 200) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise SystemExit(f"Unexpected {response.status_code}: {response.text}")
    body = response.json()
    if body.get("code") != 0:
        raise SystemExit(f"Unexpected API code: {response.text}")
    return body["data"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, username: str) -> str:
    data = require_ok(client.post("/api/v1/auth/login", json={"username": username, "password": "Test@1234"}))
    print_step(f"{username} logged in", data["user"])
    return data["access_token"]


def main() -> None:
    app.state.store.reset()
    client = TestClient(app)

    print_step("Demo main flow started", {"base_url": "in-process TestClient", "password": "Test@1234"})
    teacher = login(client, "teacher")
    student = login(client, "student")

    problem = require_ok(
        client.post(
            "/api/v1/problems/",
            headers=auth(teacher),
            json={
                "problem_text": "325 + 47 = ___",
                "problem_type": "arithmetic",
                "reference_answer": "372",
                "grade_level": 3,
                "difficulty": "medium",
                "curriculum_version": "人教版",
                "solution_steps": ["个位 5+7=12，写2进1", "十位 2+4+1=7", "百位写3"],
                "tags": ["加法", "进位"],
            },
        ),
        expected_status=201,
    )
    problem_id = problem["problem_id"]
    print_step("Teacher created a problem", problem)

    assignment = require_ok(
        client.post(
            "/api/v1/assignments/",
            headers=auth(teacher),
            json={
                "title": "演示作业：三位数加法",
                "class_ids": ["class-3a"],
                "due_date": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "problem_ids": [problem_id],
            },
        ),
        expected_status=201,
    )
    assignment_id = assignment["assignment_id"]
    print_step("Teacher assigned it to class-3a", assignment)

    student_assignments = require_ok(client.get("/api/v1/assignments/", headers=auth(student)))
    print_step("Student can see the assignment", student_assignments["items"])

    submission = require_ok(
        client.post(
            "/api/v1/submissions/",
            headers=auth(student),
            json={"assignment_id": assignment_id, "answers": [{"problem_id": problem_id, "answer_text": "362"}]},
        ),
        expected_status=201,
    )
    print_step("Student submitted an answer and received grading", submission)

    stats = require_ok(client.get(f"/api/v1/assignments/{assignment_id}/stats", headers=auth(teacher)))
    print_step(
        "Teacher can view assignment stats",
        {
            "submitted_count": stats["submitted_count"],
            "average_accuracy": stats["average_accuracy"],
            "knowledge_point_alerts": stats["knowledge_point_alerts"],
        },
    )

    review_problem = require_ok(
        client.post(
            "/api/v1/problems/",
            headers=auth(teacher),
            json={
                "problem_text": "9 + 8 = ___",
                "problem_type": "arithmetic",
                "reference_answer": "17",
                "grade_level": 3,
                "difficulty": "easy",
                "curriculum_version": "人教版",
                "tags": ["加法"],
            },
        ),
        expected_status=201,
    )
    review_assignment = require_ok(
        client.post(
            "/api/v1/assignments/",
            headers=auth(teacher),
            json={
                "title": "演示作业：人工复核",
                "class_ids": ["class-3a"],
                "due_date": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                "problem_ids": [review_problem["problem_id"]],
            },
        ),
        expected_status=201,
    )
    pending_submission = require_ok(
        client.post(
            "/api/v1/submissions/",
            headers=auth(student),
            json={
                "assignment_id": review_assignment["assignment_id"],
                "answers": [{"problem_id": review_problem["problem_id"], "answer_text": "uncertain:17"}],
            },
        ),
        expected_status=201,
    )
    print_step("Low-confidence answer was routed to human review", pending_submission)

    queue = require_ok(client.get("/api/v1/teacher/human-review-queue", headers=auth(teacher)))
    if not queue["items"]:
        raise SystemExit("Expected a pending human review item")
    review_id = queue["items"][0]["review_id"]
    print_step("Teacher can see pending review queue", queue["items"])

    reviewed = require_ok(
        client.post(
            f"/api/v1/teacher/human-review/{review_id}",
            headers=auth(teacher),
            json={"override_correct": True, "reviewer_notes": "demo approved", "is_training_example": True},
        )
    )
    print_step("Teacher completed human review", reviewed)

    print_step(
        "Demo main flow completed",
        {
            "assignment_id": assignment_id,
            "submission_id": submission["submission_id"],
            "review_id": review_id,
        },
    )


if __name__ == "__main__":
    main()
