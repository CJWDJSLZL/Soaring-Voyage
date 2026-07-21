from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.grading import GradeRequest, route_grade
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_state() -> None:
    app.state.store.reset()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def login(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "Test@1234"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["access_token"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def create_problem(
    client: TestClient,
    token: str,
    *,
    answer: str = "372",
    problem_type: str = "arithmetic",
) -> str:
    response = client.post(
        "/api/v1/problems/",
        headers=auth(token),
        json={
            "problem_text": "测试题",
            "problem_type": problem_type,
            "reference_answer": answer,
            "grade_level": 3,
            "difficulty": "medium",
            "curriculum_version": "人教版",
            "solution_steps": ["第一步", "第二步"],
            "tags": ["审计"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["problem_id"]


def create_assignment(
    client: TestClient,
    token: str,
    problem_ids: list[str],
    *,
    class_ids: list[str] | None = None,
    due_date: datetime | None = None,
    title: str = "审计作业",
) -> str:
    response = client.post(
        "/api/v1/assignments/",
        headers=auth(token),
        json={
            "title": title,
            "class_ids": class_ids or ["class-3a"],
            "due_date": (due_date or datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "problem_ids": problem_ids,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["assignment_id"]


def submit(client: TestClient, token: str, assignment_id: str, answers: list[dict]) -> dict:
    response = client.post(
        "/api/v1/submissions/",
        headers=auth(token),
        json={"assignment_id": assignment_id, "answers": answers},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_teacher_cannot_read_other_class_submission_from_shared_assignment(client: TestClient) -> None:
    admin = login(client, "admin")
    teacher = login(client, "teacher")
    student2 = login(client, "student2")
    problem_id = create_problem(client, admin)
    assignment_id = create_assignment(client, admin, [problem_id], class_ids=["class-3a", "class-3b"])
    submission = submit(
        client,
        student2,
        assignment_id,
        [{"problem_id": problem_id, "answer_text": "uncertain:1"}],
    )

    detail = client.get(f"/api/v1/submissions/{submission['submission_id']}", headers=auth(teacher))
    assert detail.status_code == 404
    listed = client.get("/api/v1/submissions/", headers=auth(teacher)).json()["data"]["items"]
    assert all(item["submission_id"] != submission["submission_id"] for item in listed)
    queue = client.get("/api/v1/teacher/human-review-queue", headers=auth(teacher)).json()["data"]["items"]
    assert all(item["submission_id"] != submission["submission_id"] for item in queue)


def test_chinese_fill_blank_is_not_treated_as_empty() -> None:
    correct = route_grade(
        GradeRequest(
            question="加法需要什么？",
            reference_answer="进位",
            student_answer="进位",
            question_type="fill_blank",
            grade=3,
        )
    )
    wrong = route_grade(
        GradeRequest(
            question="加法需要什么？",
            reference_answer="进位",
            student_answer="借位",
            question_type="fill_blank",
            grade=3,
        )
    )
    assert correct.is_correct is True
    assert correct.source != "empty_answer"
    assert wrong.is_correct is False
    assert wrong.source != "empty_answer"
    for decorated in ("进位😀", "进位。", "（进位）"):
        result = route_grade(
            GradeRequest(
                question="加法需要什么？",
                reference_answer="进位",
                student_answer=decorated,
                question_type="fill_blank",
                grade=3,
            )
        )
        assert result.is_correct is True
    for reference, student in (
        ("充分", "充"),
        ("平均分", "平均"),
        ("平方厘米", "平方"),
        ("三角形有3个", "三角形有3"),
    ):
        result = route_grade(
            GradeRequest(
                question="文本填空",
                reference_answer=reference,
                student_answer=student,
                question_type="fill_blank",
                grade=3,
            )
        )
        assert result.is_correct is False


def test_hint_attempts_are_preserved_as_immutable_history(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    submission = submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "111"}])
    response = client.post(
        f"/api/v1/submissions/{submission['submission_id']}/hint",
        headers=auth(student),
        json={"problem_id": problem_id, "new_answer": "222"},
    )
    assert response.status_code == 200

    history = app.state.store.attempts[submission["submission_id"]][problem_id]
    assert [item["attempt_number"] for item in history] == [1, 2]
    assert [item["student_answer"] for item in history] == ["111", "222"]


def test_expired_assignment_has_consistent_status_and_cannot_be_edited(client: TestClient) -> None:
    teacher = login(client, "teacher")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    app.state.store.assignments[assignment_id]["due_date"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()

    detail = client.get(f"/api/v1/assignments/{assignment_id}", headers=auth(teacher))
    listed = client.get("/api/v1/assignments/", headers=auth(teacher))
    patched = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"title": "不应成功"},
    )
    assert detail.json()["data"]["status"] == "expired"
    item = next(item for item in listed.json()["data"]["items"] if item["assignment_id"] == assignment_id)
    assert item["status"] == "expired"
    assert patched.status_code == 409


def test_final_hint_returns_full_solution_and_locked_state(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    submission = submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "0"}])

    data = None
    for _ in range(3):
        response = client.post(
            f"/api/v1/submissions/{submission['submission_id']}/hint",
            headers=auth(student),
            json={"problem_id": problem_id, "new_answer": "0"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
    assert data is not None
    assert data["show_full_solution"] is True
    assert data["locked"] is True
    assert data["solution_steps"] == ["第一步", "第二步"]
    assert data["next_hint"] is None
    assert data["knowledge_point_recorded"] is True
    final_attempt = app.state.store.attempts[submission["submission_id"]][problem_id][-1]
    assert final_attempt["knowledge_point_recorded"] is True
    assert final_attempt["knowledge_point_record"]["record_id"] == data["knowledge_point_record"]["record_id"]


def test_human_override_respects_training_example_opt_out(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "uncertain:1"}])
    review = client.get("/api/v1/teacher/human-review-queue", headers=auth(teacher)).json()["data"]["items"][0]
    response = client.post(
        f"/api/v1/teacher/human-review/{review['review_id']}",
        headers=auth(teacher),
        json={"override_correct": False, "override_error_type": "计算错误", "is_training_example": False},
    )
    assert response.status_code == 200
    assert response.json()["data"]["is_training_example"] is False
    assert app.state.store.reviews[review["review_id"]]["is_training_example"] is False


def test_assignment_list_validates_sorting_and_returns_contract_fields(client: TestClient) -> None:
    teacher = login(client, "teacher")
    problem_id = create_problem(client, teacher)
    create_assignment(
        client,
        teacher,
        [problem_id],
        due_date=datetime.now(UTC) + timedelta(days=2),
        title="晚截止",
    )
    create_assignment(
        client,
        teacher,
        [problem_id],
        due_date=datetime.now(UTC) + timedelta(hours=1),
        title="早截止",
    )

    response = client.get(
        "/api/v1/assignments/?order_by=due_date&order=asc",
        headers=auth(teacher),
    )
    assert response.status_code == 200
    items = response.json()["data"]["items"]
    assert [item["title"] for item in items] == ["早截止", "晚截止"]
    assert {"class_name", "is_expiring_soon"}.issubset(items[0])
    assert client.get("/api/v1/assignments/?status=nonsense", headers=auth(teacher)).status_code == 422
    assert client.get("/api/v1/assignments/?order_by=nonsense", headers=auth(teacher)).status_code == 422


def test_unanswered_problem_can_be_removed_after_another_problem_was_submitted(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    answered = create_problem(client, teacher, answer="1")
    unanswered = create_problem(client, teacher, answer="2")
    assignment_id = create_assignment(client, teacher, [answered])
    submit(client, student, assignment_id, [{"problem_id": answered, "answer_text": "1"}])
    added = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"add_problem_ids": [unanswered]},
    )
    assert added.status_code == 200
    removed = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        headers=auth(teacher),
        json={"remove_problem_ids": [unanswered]},
    )
    assert removed.status_code == 200


def test_pending_review_header_is_independent_of_status_filter(client: TestClient) -> None:
    teacher = login(client, "teacher")
    student = login(client, "student")
    problem_id = create_problem(client, teacher)
    assignment_id = create_assignment(client, teacher, [problem_id])
    submit(client, student, assignment_id, [{"problem_id": problem_id, "answer_text": "uncertain:1"}])

    response = client.get("/api/v1/teacher/human-review-queue?status=reviewed", headers=auth(teacher))
    assert response.status_code == 200
    assert response.headers["X-Pending-Review-Count"] == "1"
    assert response.json()["data"]["items"] == []
