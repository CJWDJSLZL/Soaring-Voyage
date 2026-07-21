from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.main import app
from fastapi.testclient import TestClient
from tests.api.test_audit_regressions import auth, create_assignment, create_problem, login, submit


@pytest.fixture(autouse=True)
def reset_state() -> None:
    app.state.store.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_teacher_must_own_every_existing_assignment_class_to_patch(client: TestClient) -> None:
    admin = login(client, "admin")
    sysadmin = login(client, "sysadmin")
    teacher = login(client, "teacher")
    problem_id = create_problem(client, admin)
    assignment_id = create_assignment(client, admin, [problem_id], class_ids=["class-3a", "class-3b"])

    denied = client.patch(f"/api/v1/assignments/{assignment_id}", headers=auth(teacher), json={"title": "越权修改"})
    assert denied.status_code == 403
    assert app.state.store.assignments[assignment_id]["title"] != "越权修改"

    assert (
        client.patch(
            f"/api/v1/assignments/{assignment_id}", headers=auth(admin), json={"title": "管理员修改"}
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/assignments/{assignment_id}", headers=auth(sysadmin), json={"title": "系统管理员修改"}
        ).status_code
        == 200
    )


def test_naive_dates_are_utc_and_due_sort_uses_actual_instants(client: TestClient) -> None:
    teacher = login(client, "teacher")
    problem_id = create_problem(client, teacher)
    first_id = create_assignment(client, teacher, [problem_id], title="实际较晚")
    second_id = create_assignment(client, teacher, [problem_id], title="实际较早")
    now = datetime.now(UTC)
    later = now + timedelta(hours=2)
    earlier = now + timedelta(hours=1)
    app.state.store.assignments[first_id]["due_date"] = later.astimezone(timezone(timedelta(hours=-10))).isoformat()
    app.state.store.assignments[second_id]["due_date"] = earlier.isoformat()

    sorted_response = client.get("/api/v1/assignments/?order_by=due_date&order=asc", headers=auth(teacher))
    assert sorted_response.status_code == 200
    assert [item["title"] for item in sorted_response.json()["data"]["items"]] == ["实际较早", "实际较晚"]

    app.state.store.assignments[first_id]["due_date"] = (
        (datetime.now(UTC) + timedelta(hours=2)).replace(tzinfo=None).isoformat()
    )
    naive_response = client.get("/api/v1/assignments/", headers=auth(teacher))
    assert naive_response.status_code == 200
    assert (
        next(item for item in naive_response.json()["data"]["items"] if item["assignment_id"] == first_id)["status"]
        == "active"
    )


def test_empty_final_hint_locks_exposes_solution_and_persists_error_record(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    submission = submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "0"}])

    response = None
    for answer in ("0", "0", ""):
        response = client.post(
            f"/api/v1/submissions/{submission['submission_id']}/hint",
            headers=auth(student),
            json={"problem_id": problem_id, "new_answer": answer},
        )
    assert response is not None and response.status_code == 200
    data = response.json()["data"]
    assert data["locked"] is True
    assert data["show_full_solution"] is True
    assert data["solution_steps"] == ["第一步", "第二步"]
    assert data["hint_state"] == "solution"
    assert data["knowledge_point_recorded"] is True
    record = data["knowledge_point_record"]
    assert record["record_type"] == "error_history"
    assert record["student_id"] == "user-student"
    assert record["problem_id"] == problem_id
    assert record["error_type"] == "未作答"
    assert app.state.store.knowledge_records[record["record_id"]] == record


def test_hint_attempt_is_rejected_after_naive_utc_deadline(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    submission = submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "0"}])
    app.state.store.assignments[assignment_id]["due_date"] = (
        (datetime.now(UTC) - timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    )

    response = client.post(
        f"/api/v1/submissions/{submission['submission_id']}/hint",
        headers=auth(student),
        json={"problem_id": problem_id, "new_answer": "1"},
    )
    assert response.status_code == 410
    assert len(app.state.store.attempts[submission["submission_id"]][problem_id]) == 1


def test_assignment_role_defaults_and_explicit_sort_parameters(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    later_id = create_assignment(
        client, teacher, [problem_id], due_date=datetime.now(UTC) + timedelta(days=2), title="先创建晚截止"
    )
    earlier_id = create_assignment(
        client, teacher, [problem_id], due_date=datetime.now(UTC) + timedelta(days=1), title="后创建早截止"
    )

    student_default = client.get("/api/v1/assignments/", headers=auth(student)).json()["data"]["items"]
    teacher_default = client.get("/api/v1/assignments/", headers=auth(teacher)).json()["data"]["items"]
    explicit_due_desc = client.get("/api/v1/assignments/?order_by=due_date&order=desc", headers=auth(student)).json()[
        "data"
    ]["items"]
    explicit_created_asc = client.get(
        "/api/v1/assignments/?order_by=created_at&order=asc", headers=auth(student)
    ).json()["data"]["items"]

    assert [item["assignment_id"] for item in student_default] == [earlier_id, later_id]
    assert [item["assignment_id"] for item in teacher_default] == [earlier_id, later_id]
    assert [item["assignment_id"] for item in explicit_due_desc] == [later_id, earlier_id]
    assert [item["assignment_id"] for item in explicit_created_asc] == [later_id, earlier_id]


def test_repository_class_names_are_used_in_assignment_and_review_responses(client: TestClient) -> None:
    admin = login(client, "admin")
    student = login(client, "student")
    problem_id = create_problem(client, admin)
    created = client.post(
        "/api/v1/assignments/",
        headers=auth(admin),
        json={
            "title": "班名测试",
            "class_ids": ["class-3a"],
            "due_date": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "problem_ids": [problem_id],
        },
    )
    assignment_id = created.json()["data"]["assignment_id"]
    assert created.json()["data"]["classes"] == [{"class_id": "class-3a", "class_name": "三年级A班"}]
    detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=auth(student))
    assert detail.json()["data"]["class_name"] == "三年级A班"

    submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "uncertain:0"}])
    review = client.get("/api/v1/teacher/human-review-queue", headers=auth(admin)).json()["data"]["items"][0]
    assert review["class_name"] == "三年级A班"
