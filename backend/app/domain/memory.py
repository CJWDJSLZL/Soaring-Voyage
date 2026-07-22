from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.config import settings
from app.core.errors import utcnow
from app.core.security import hash_password
from app.domain.models import JsonDict, Ticket, User


class InMemoryRepository:
    """Executable Phase-1 repository; its public shape is asyncpg-adapter friendly."""

    def __init__(self) -> None:
        if not settings.is_development:
            raise RuntimeError("InMemoryRepository is restricted to test/development")
        common = hash_password("Test@1234")
        self._seed_users = {
            "student": User("user-student", "student", "演示学生", common, "student", "tenant-demo", ["class-3a"], 3),
            "student2": User(
                "user-student2", "student2", "其他班学生", common, "student", "tenant-demo", ["class-3b"], 3
            ),
            "teacher": User("user-teacher", "teacher", "演示教师", common, "teacher", "tenant-demo", ["class-3a"]),
            "admin": User("user-admin", "admin", "学校管理员", common, "admin", "tenant-demo"),
            "sysadmin": User("user-sysadmin", "sysadmin", "系统管理员", common, "sysadmin", "tenant-demo"),
        }
        self._seed_classes: dict[str, JsonDict] = {
            "class-3a": {"class_id": "class-3a", "tenant_id": "tenant-demo", "class_name": "三年级A班"},
            "class-3b": {"class_id": "class-3b", "tenant_id": "tenant-demo", "class_name": "三年级B班"},
        }
        self._seed_classes["class-3a"]["grade_level"] = 3
        self._seed_classes["class-3b"]["grade_level"] = 3
        self.reset()

    def reset(self) -> None:
        self.users: dict[str, User] = deepcopy(self._seed_users)
        self.classes: dict[str, JsonDict] = deepcopy(self._seed_classes)
        self.problems: dict[str, JsonDict] = {}
        self.assignments: dict[str, JsonDict] = {}
        self.submissions: dict[str, JsonDict] = {}
        self.attempts: dict[str, dict[str, list[JsonDict]]] = {}
        self.reviews: dict[str, JsonDict] = {}
        self.knowledge_records: dict[str, JsonDict] = {}
        self.tickets: dict[str, Ticket] = {}
        self.events: dict[str, list[JsonDict]] = {}
        self.harness_runs: dict[str, JsonDict] = {}
        self.jobs: dict[str, JsonDict] = {}
        self.disabled_user_ids: set[str] = set()

    def user_by_id(self, user_id: str) -> User | None:
        return next((user for user in self.users.values() if user.user_id == user_id), None)

    def known_class_ids(self, tenant_id: str) -> set[str]:
        return {class_id for class_id, item in self.classes.items() if item["tenant_id"] == tenant_id}

    def class_name(self, tenant_id: str, class_id: str) -> str:
        item = self.classes.get(class_id)
        if item is None or item["tenant_id"] != tenant_id:
            raise KeyError(class_id)
        return str(item["class_name"])

    def purge_expired_tickets(self) -> int:
        expired = [value for value, ticket in self.tickets.items() if ticket.expires_at <= utcnow()]
        for value in expired:
            del self.tickets[value]
        return len(expired)

    def submission_for(self, student_id: str, assignment_id: str) -> JsonDict | None:
        return next(
            (
                item
                for item in self.submissions.values()
                if item["student_id"] == student_id and item["assignment_id"] == assignment_id
            ),
            None,
        )

    async def identity_by_username(self, username: str) -> User | None:
        user = self.users.get(username)
        if user is None or user.user_id in self.disabled_user_ids:
            return None
        return user

    async def identity_by_id(self, user_id: str, tenant_id: str, role: str) -> User | None:
        user = self.user_by_id(user_id)
        if user is None or user.tenant_id != tenant_id or user.role != role or user.user_id in self.disabled_user_ids:
            return None
        return user

    async def register_login_failure(self, user: User, *, max_failures: int, locked_until: datetime) -> User:
        user.failed_logins += 1
        if user.failed_logins >= max_failures:
            user.locked_until = locked_until
        return user

    async def clear_login_failures(self, user: User) -> None:
        user.failed_logins = 0
        user.locked_until = None

    async def replace_password(self, user: User, password_hash: bytes) -> None:
        user.password_hash = password_hash
        user.token_version += 1
        user.force_change_password = False

    async def increment_token_version(self, user: User) -> None:
        user.token_version += 1

    async def create_catalog_problem(self, user: User, problem: dict[str, Any]) -> str:
        problem_id = str(uuid4())
        self.problems[problem_id] = {
            "problem_id": problem_id,
            "tenant_id": user.tenant_id,
            "created_by": user.user_id,
            "embedding_id": None,
            "embedding_status": "pending",
            **problem,
            "created_at": utcnow().isoformat(),
        }
        return problem_id

    async def bulk_import_problems(self, user: User, rows: list[dict[str, Any]], payload: dict[str, Any]) -> JsonDict:
        job_id = str(uuid4())
        now = utcnow().isoformat()
        created_ids: list[str] = []
        failed_rows: list[JsonDict] = []
        for row in rows:
            try:
                problem_id = await self.create_catalog_problem(
                    user,
                    {
                        "problem_text": row["problem_text"],
                        "problem_type": row["problem_type"],
                        "reference_answer": row["reference_answer"],
                        "grade_level": row["grade_level"],
                        "difficulty": row["difficulty"],
                        "curriculum_version": payload["curriculum_version"],
                        "solution_steps": row.get("solution_steps", []),
                        "common_errors": row.get("common_errors", []),
                        "tags": row.get("tags", []),
                    },
                )
                created_ids.append(problem_id)
            except Exception as exc:
                failed_rows.append({"row": row["row"], "problem_text": row["problem_text"], "reason": str(exc)})
        result = {
            "total": len(rows),
            "success": len(created_ids),
            "failed": len(failed_rows),
            "problem_ids": created_ids,
            "failed_rows": failed_rows,
        }
        self.jobs[job_id] = {
            "job_id": job_id,
            "job_type": "bulk_import_problems",
            "status": "succeeded" if not failed_rows else "failed",
            "progress": 1.0,
            "created_at": now,
            "updated_at": now,
            "created_by": user.user_id,
            "payload": payload,
            "result": result,
        }
        return {"import_job_id": job_id, "status": self.jobs[job_id]["status"], **result}

    async def list_catalog_problems(
        self,
        user: User,
        *,
        grade_level: int | None,
        problem_type: str | None,
        difficulty: str | None,
        keyword: str | None,
        page_number: int,
        page_size: int,
    ) -> JsonDict:
        items = [
            item
            for item in self.problems.values()
            if item.get("tenant_id") == user.tenant_id and not item.get("is_deleted")
        ]
        if grade_level is not None:
            items = [item for item in items if item["grade_level"] == grade_level]
        if problem_type is not None:
            items = [item for item in items if item["problem_type"] == problem_type]
        if difficulty is not None:
            items = [item for item in items if item["difficulty"] == difficulty]
        if keyword is not None:
            lowered = keyword.lower()
            items = [item for item in items if lowered in str(item["problem_text"]).lower()]
        items.sort(key=lambda item: (str(item.get("created_at", "")), str(item["problem_id"])), reverse=True)
        return self._page(items, page_number, page_size)

    async def delete_catalog_problem(self, user: User, problem_id: str) -> JsonDict:
        from app.core.errors import AppError

        problem = self.problems.get(problem_id)
        if problem is None or problem["tenant_id"] != user.tenant_id or problem.get("is_deleted"):
            raise AppError(404, 4004, "题目不存在")
        if user.role == "teacher" and problem.get("created_by") != user.user_id:
            raise AppError(403, 4003, "只能删除自己创建的题目")
        referenced = [
            assignment_id
            for assignment_id, assignment in self.assignments.items()
            if assignment["tenant_id"] == user.tenant_id and problem_id in assignment["problem_ids"]
        ]
        if referenced:
            raise AppError(409, 4005, "该题目已在作业中使用，无法删除", {"assignment_ids": referenced})
        problem["is_deleted"] = True
        problem["deleted_at"] = utcnow().isoformat()
        problem["deleted_by"] = user.user_id
        return {"problem_id": problem_id, "deleted": True}

    @staticmethod
    def _page(items: list[JsonDict], page_number: int, page_size: int) -> JsonDict:
        total = len(items)
        start = (page_number - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "total": total,
            "page": page_number,
            "page_size": page_size,
            "has_next": start + page_size < total,
        }

    @staticmethod
    def _assignment_status(assignment: JsonDict) -> str:
        due_date = assignment.get("due_date")
        if due_date:
            parsed = datetime.fromisoformat(str(due_date))
            due = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            if due.astimezone(UTC) <= utcnow():
                return "expired"
        return "active"

    async def create_assignment(self, user: User, payload: dict[str, Any]) -> JsonDict:
        if len(payload["class_ids"]) != len(set(payload["class_ids"])):
            from app.core.errors import AppError

            raise AppError(422, 4022, "请求参数校验失败", "class_ids must be unique")
        if not set(payload["class_ids"]).issubset(self.known_class_ids(user.tenant_id)):
            from app.core.errors import AppError

            raise AppError(404, 4004, "班级不存在")
        if user.role == "teacher" and not set(payload["class_ids"]).issubset(user.class_ids):
            from app.core.errors import AppError

            raise AppError(403, 4003, "教师只能向本人班级布置作业")
        missing = [
            pid
            for pid in payload["problem_ids"]
            if pid not in self.problems or self.problems[pid]["tenant_id"] != user.tenant_id
        ]
        if missing:
            from app.core.errors import AppError

            raise AppError(404, 4004, "题目不存在", f"Missing problem ids: {missing}")
        assignment_id = str(uuid4())
        created_at = utcnow().isoformat()
        due_date = (
            payload["due_date"].isoformat()
            if isinstance(payload.get("due_date"), datetime)
            else payload.get("due_date")
        )
        self.assignments[assignment_id] = {
            "assignment_id": assignment_id,
            "tenant_id": user.tenant_id,
            "created_by": user.user_id,
            "title": payload["title"],
            "class_ids": list(payload["class_ids"]),
            "problem_ids": list(payload["problem_ids"]),
            "due_date": due_date,
            "created_at": created_at,
            "status": "active",
        }
        return {
            "assignment_id": assignment_id,
            "title": payload["title"],
            "classes": [
                {"class_id": item, "class_name": self.class_name(user.tenant_id, item)} for item in payload["class_ids"]
            ],
            "due_date": due_date,
            "problem_count": len(payload["problem_ids"]),
            "created_at": created_at,
            "status": "active",
        }

    @staticmethod
    def _sort_key(item: JsonDict, order_by: str) -> str | datetime:
        value = item.get(order_by)
        if order_by == "due_date" and value:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return str(value or "")

    async def list_assignments(
        self,
        user: User,
        *,
        class_id: str | None,
        status: str,
        order_by: str,
        order: str,
        page_number: int,
        page_size: int,
    ) -> JsonDict:
        items: list[JsonDict] = []
        for assignment in self.assignments.values():
            visible = user.tenant_id == assignment["tenant_id"] and (
                user.role in {"admin", "sysadmin"} or bool(set(user.class_ids) & set(assignment["class_ids"]))
            )
            if not visible or (class_id and class_id not in assignment["class_ids"]):
                continue
            current_status = self._assignment_status(assignment)
            if status != "all" and current_status != status:
                continue
            due = assignment["due_date"]
            item = {
                "assignment_id": assignment["assignment_id"],
                "title": assignment["title"],
                "class_name": "、".join(self.class_name(user.tenant_id, cid) for cid in assignment["class_ids"]),
                "due_date": due,
                "problem_count": len(assignment["problem_ids"]),
                "status": current_status,
                "is_expiring_soon": False,
                "created_at": assignment["created_at"],
            }
            if user.role == "student":
                mine = self.submission_for(user.user_id, assignment["assignment_id"])
                item["submission_status"] = mine["status"] if mine else "not_submitted"
            items.append(item)
        items.sort(key=lambda item: self._sort_key(item, order_by), reverse=order == "desc")
        return self._page(items, page_number, page_size)

    async def assignment_detail(self, user: User, assignment_id: str) -> JsonDict:
        from app.core.errors import AppError

        assignment = self.assignments.get(assignment_id)
        if (
            assignment is None
            or assignment["tenant_id"] != user.tenant_id
            or not (user.role in {"admin", "sysadmin"} or bool(set(user.class_ids) & set(assignment["class_ids"])))
        ):
            raise AppError(404, 4004, "作业不存在")
        fields = ["problem_id", "problem_text", "problem_type", "grade_level", "difficulty", "tags"]
        if user.role == "student":
            fields = ["problem_id", "problem_text", "problem_type", "difficulty"]
        problems = []
        for sequence, problem_id in enumerate(assignment["problem_ids"], 1):
            problem = self.problems[problem_id]
            problems.append({"sequence": sequence, **{key: problem[key] for key in fields}})
        data = {
            "assignment_id": assignment_id,
            "title": assignment["title"],
            "class_name": "、".join(self.class_name(user.tenant_id, cid) for cid in assignment["class_ids"]),
            "due_date": assignment["due_date"],
            "status": self._assignment_status(assignment),
            "problems": problems,
        }
        if user.role == "student":
            mine = self.submission_for(user.user_id, assignment_id)
            data["my_submission"] = {"submission_id": mine["submission_id"], "status": mine["status"]} if mine else None
        return data

    async def assignment_stats(self, user: User, assignment_id: str) -> JsonDict:
        from app.core.errors import AppError

        assignment = self.assignments.get(assignment_id)
        if (
            assignment is None
            or assignment["tenant_id"] != user.tenant_id
            or not (user.role in {"admin", "sysadmin"} or bool(set(user.class_ids) & set(assignment["class_ids"])))
        ):
            raise AppError(404, 4004, "作业不存在")
        class_ids = set(assignment["class_ids"])
        total_students = sum(
            item.tenant_id == user.tenant_id and item.role == "student" and bool(set(item.class_ids) & class_ids)
            for item in self.users.values()
        )
        submissions = [
            item
            for item in self.submissions.values()
            if item["tenant_id"] == user.tenant_id and item["assignment_id"] == assignment_id
        ]
        latest_results = [result for submission in submissions for result in submission["results"]]
        correct = sum(result.get("is_correct") is True for result in latest_results)
        problem_stats: list[JsonDict] = []
        for sequence, problem_id in enumerate(assignment["problem_ids"], 1):
            problem = self.problems[problem_id]
            attempts = [
                attempt
                for submission in submissions
                for attempt in self.attempts.get(submission["submission_id"], {}).get(problem_id, [])
            ]
            latest_by_submission = [
                next((item for item in submission["results"] if item["problem_id"] == problem_id), None)
                for submission in submissions
            ]
            latest = [item for item in latest_by_submission if item is not None]
            error_counts: dict[str, int] = {}
            for result in latest:
                if result.get("error_type"):
                    error_counts[str(result["error_type"])] = error_counts.get(str(result["error_type"]), 0) + 1
            answered = len(latest)
            problem_stats.append(
                {
                    "problem_id": problem_id,
                    "sequence": sequence,
                    "problem_text": problem["problem_text"],
                    "total_attempts": len(attempts) or answered,
                    "correct_first_try": sum(
                        attempt.get("attempt_number") == 1 and attempt.get("is_correct") is True for attempt in attempts
                    ),
                    "correct_after_hint": sum(
                        result.get("attempt_number", 1) > 1 and result.get("is_correct") is True for result in latest
                    ),
                    "still_wrong": sum(result.get("is_correct") is False for result in latest),
                    "pending_review": sum(result.get("routed_to_human") for result in latest),
                    "accuracy_first_try": round(
                        sum(
                            attempt.get("attempt_number") == 1 and attempt.get("is_correct") is True
                            for attempt in attempts
                        )
                        / answered,
                        3,
                    )
                    if answered
                    else 0.0,
                    "top_error_types": [
                        {
                            "error_type": key,
                            "count": value,
                            "percentage": round(value / answered, 3) if answered else 0.0,
                        }
                        for key, value in sorted(error_counts.items(), key=lambda item: item[1], reverse=True)[:5]
                    ],
                    "avg_hint_used": round(sum(result.get("hint_level", 0) for result in latest) / answered, 3)
                    if answered
                    else 0.0,
                }
            )
        error_distribution: dict[str, int] = {}
        for result in latest_results:
            if result.get("error_type"):
                error_distribution[str(result["error_type"])] = error_distribution.get(str(result["error_type"]), 0) + 1
        return {
            "assignment_id": assignment_id,
            "total_students": total_students,
            "submitted_count": len(submissions),
            "submission_rate": round(len(submissions) / total_students, 3) if total_students else 0.0,
            "average_accuracy": round(correct / len(latest_results), 3) if latest_results else 0.0,
            "problem_stats": problem_stats,
            "error_distribution": error_distribution,
            "knowledge_point_alerts": [],
        }

    async def create_class(self, user: User, payload: dict[str, Any]) -> JsonDict:
        from app.core.errors import AppError

        teacher = self.user_by_id(payload["teacher_id"])
        if teacher is None or teacher.tenant_id != user.tenant_id or teacher.role != "teacher":
            raise AppError(404, 4004, "教师不存在")
        class_id = str(uuid4())
        created_at = utcnow().isoformat()
        self.classes[class_id] = {
            "class_id": class_id,
            "tenant_id": user.tenant_id,
            "class_name": payload["name"],
            "grade_level": payload["grade_level"],
            "teacher_id": payload["teacher_id"],
            "academic_year": payload["academic_year"],
            "created_at": created_at,
        }
        if class_id not in teacher.class_ids:
            teacher.class_ids.append(class_id)
        return {
            "class_id": class_id,
            "name": payload["name"],
            "grade_level": payload["grade_level"],
            "teacher_id": payload["teacher_id"],
            "academic_year": payload["academic_year"],
            "created_at": created_at,
        }

    async def bulk_create_students(self, user: User, rows: list[dict[str, Any]]) -> JsonDict:
        class_by_name = {
            str(item.get("class_name") or item.get("name")): (class_id, item)
            for class_id, item in self.classes.items()
            if item["tenant_id"] == user.tenant_id
        }
        created = 0
        skipped = 0
        failed = 0
        skipped_reasons: list[JsonDict] = []
        failed_rows: list[JsonDict] = []
        seen_usernames: set[str] = set()
        for row in rows:
            username = row["username"]
            class_entry = class_by_name.get(row["class_name"])
            if username in seen_usernames:
                failed += 1
                failed_rows.append(
                    {"row": row["row"], "username": username, "reason": "file contains duplicate username"}
                )
                continue
            seen_usernames.add(username)
            if class_entry is None:
                failed += 1
                failed_rows.append({"row": row["row"], "username": username, "reason": "class does not exist"})
                continue
            class_id, class_item = class_entry
            if int(class_item["grade_level"]) != int(row["grade_level"]):
                failed += 1
                failed_rows.append({"row": row["row"], "username": username, "reason": "grade does not match class"})
                continue
            existing = self.users.get(username)
            if existing is not None and existing.tenant_id == user.tenant_id:
                skipped += 1
                skipped_reasons.append({"row": row["row"], "username": username, "reason": "username already exists"})
                continue
            student_id = str(uuid4())
            self.users[username] = User(
                student_id,
                username,
                row["display_name"],
                row["password_hash"],
                "student",
                user.tenant_id,
                [class_id],
                int(row["grade_level"]),
            )
            created += 1
        return {
            "created": created,
            "skipped": skipped,
            "failed": failed,
            "skipped_reasons": skipped_reasons,
            "failed_rows": failed_rows,
        }

    async def admin_stats_overview(self, user: User) -> JsonDict:
        tenant_classes = [item for item in self.classes.values() if item["tenant_id"] == user.tenant_id]
        tenant_submissions = [item for item in self.submissions.values() if item["tenant_id"] == user.tenant_id]
        latest_results = [result for submission in tenant_submissions for result in submission["results"]]
        human_review_count = sum(result.get("grading_source") == "human_override" for result in latest_results)
        rule_fallback_count = sum(result.get("grading_source") == "rule_fallback" for result in latest_results)
        correct = sum(result.get("is_correct") is True for result in latest_results)
        reviewed = [review for review in self.reviews.values() if review["tenant_id"] == user.tenant_id]
        return {
            "tenant_name": user.tenant_id,
            "active_school_year": "development",
            "users": {
                "total_students": sum(
                    item.tenant_id == user.tenant_id and item.role == "student" for item in self.users.values()
                ),
                "total_teachers": sum(
                    item.tenant_id == user.tenant_id and item.role == "teacher" for item in self.users.values()
                ),
                "total_classes": len(tenant_classes),
                "active_students_today": 0,
                "active_teachers_today": 0,
            },
            "submissions": {
                "total_all_time": len(tenant_submissions),
                "today": len(tenant_submissions),
                "this_week": len(tenant_submissions),
                "this_month": len(tenant_submissions),
            },
            "grading": {
                "ai_graded_count": len(latest_results) - human_review_count,
                "human_review_count": human_review_count or len(reviewed),
                "human_review_rate": round((human_review_count or len(reviewed)) / len(latest_results), 3)
                if latest_results
                else 0.0,
                "average_accuracy": round(correct / len(latest_results), 3) if latest_results else 0.0,
                "rule_fallback_rate": round(rule_fallback_count / len(latest_results), 3) if latest_results else 0.0,
            },
            "performance": {"avg_grading_latency_ms": 0, "p95_grading_latency_ms": 0},
        }

    async def teacher_dashboard(
        self, user: User, *, class_id: str | None, assignment_id: str | None, days: int
    ) -> JsonDict:
        from app.core.errors import AppError

        if class_id is not None:
            class_item = self.classes.get(class_id)
            if class_item is None or class_item["tenant_id"] != user.tenant_id:
                raise AppError(404, 4004, "班级不存在")
            if user.role == "teacher" and class_id not in user.class_ids:
                raise AppError(403, 4003, "权限不足")
            visible_class_ids = {class_id}
            class_name = str(class_item["class_name"])
        elif user.role == "teacher":
            visible_class_ids = set(user.class_ids)
            class_name = "、".join(self.class_name(user.tenant_id, value) for value in sorted(visible_class_ids))
        else:
            visible_class_ids = {
                value
                for value, item in self.classes.items()
                if item["tenant_id"] == user.tenant_id and not item.get("is_deleted")
            }
            class_name = "全校"

        cutoff = utcnow() - timedelta(days=days)
        submissions = [
            submission
            for submission in self.submissions.values()
            if submission["tenant_id"] == user.tenant_id
            and bool(set(submission["class_ids"]) & visible_class_ids)
            and (assignment_id is None or submission["assignment_id"] == assignment_id)
            and self._as_datetime(submission["submitted_at"]) >= cutoff
        ]
        if assignment_id is not None:
            assignment = self.assignments.get(assignment_id)
            if (
                assignment is None
                or assignment["tenant_id"] != user.tenant_id
                or not bool(set(assignment["class_ids"]) & visible_class_ids)
            ):
                raise AppError(404, 4004, "作业不存在")

        latest_results = [result for submission in submissions for result in submission["results"]]
        total_students = sum(
            item.tenant_id == user.tenant_id
            and item.role == "student"
            and bool(set(item.class_ids) & visible_class_ids)
            for item in self.users.values()
        )
        expected_submission_count = total_students
        if assignment_id is None:
            assignment_count = sum(
                item["tenant_id"] == user.tenant_id and bool(set(item["class_ids"]) & visible_class_ids)
                for item in self.assignments.values()
            )
            expected_submission_count *= assignment_count
        correct = sum(result.get("is_correct") is True for result in latest_results)
        error_distribution = self._error_distribution(latest_results)
        return {
            "class_name": class_name,
            "period": {
                "days": days,
                "start_date": cutoff.date().isoformat(),
                "end_date": utcnow().date().isoformat(),
            },
            "overview": {
                "total_submissions": len(submissions),
                "average_accuracy": round(correct / len(latest_results), 3) if latest_results else 0.0,
                "submission_rate": round(len(submissions) / expected_submission_count, 3)
                if expected_submission_count
                else 0.0,
                "human_review_rate": round(
                    sum(bool(result.get("routed_to_human")) for result in latest_results) / len(latest_results), 3
                )
                if latest_results
                else 0.0,
            },
            "error_distribution": error_distribution,
            "knowledge_point_alerts": self._knowledge_point_alerts(submissions),
            "students_needing_attention": self._students_needing_attention(submissions),
            "pending_review_count": sum(
                review["tenant_id"] == user.tenant_id and review["status"] == "pending"
                for review in self.reviews.values()
            ),
            "accuracy_trend": self._accuracy_trend(submissions),
        }

    async def student_analytics(self, user: User, student_id: str, *, days: int) -> JsonDict:
        from app.core.errors import AppError

        student = self.user_by_id(student_id)
        if student is None or student.tenant_id != user.tenant_id or student.role != "student":
            raise AppError(404, 4004, "学生不存在")
        if user.role == "teacher" and not (set(user.class_ids) & set(student.class_ids)):
            raise AppError(403, 4003, "权限不足")
        cutoff = utcnow() - timedelta(days=days)
        submissions = [
            submission
            for submission in self.submissions.values()
            if submission["tenant_id"] == user.tenant_id
            and submission["student_id"] == student_id
            and self._as_datetime(submission["submitted_at"]) >= cutoff
        ]
        latest_results = [result for submission in submissions for result in submission["results"]]
        correct = sum(result.get("is_correct") is True for result in latest_results)
        wrong_results = [result for result in latest_results if result.get("is_correct") is False]
        total_hints = sum(int(result.get("hint_level", 0)) for result in latest_results)
        class_name = "、".join(self.class_name(user.tenant_id, value) for value in student.class_ids)
        weak_points = []
        for tag, info in self._weak_point_counts(submissions).items():
            weak_points.append(
                {
                    "point": tag,
                    "error_count": info["count"],
                    "last_error_at": info["last_error_at"],
                    "trend": "stable",
                }
            )
        weak_points.sort(key=lambda item: item["error_count"], reverse=True)
        return {
            "student_name": student.display_name,
            "grade_level": student.grade_level,
            "class_name": class_name,
            "period_days": days,
            "total_submissions": len(submissions),
            "total_problems_answered": len(latest_results),
            "overall_accuracy": round(correct / len(latest_results), 3) if latest_results else 0.0,
            "accuracy_trend": self._accuracy_trend(submissions, by_day=True),
            "weak_knowledge_points": weak_points[:5],
            "hint_usage": {
                "total_hints_used": total_hints,
                "hint_dependency_rate": round(
                    sum(int(result.get("hint_level", 0)) > 0 for result in latest_results) / len(latest_results), 3
                )
                if latest_results
                else 0.0,
                "max_hint_reached_count": sum(int(result.get("hint_level", 0)) >= 3 for result in latest_results),
                "average_hints_per_wrong_answer": round(total_hints / len(wrong_results), 3) if wrong_results else 0.0,
            },
            "error_type_breakdown": self._error_distribution(latest_results),
        }

    async def assignment_export(self, user: User, assignment_id: str) -> JsonDict:
        stats = await self.assignment_stats(user, assignment_id)
        assignment = self.assignments[assignment_id]
        class_ids = set(assignment["class_ids"])
        student_rows = []
        for submission in sorted(
            self.submissions.values(), key=lambda item: (str(item["submitted_at"]), str(item["student_id"]))
        ):
            if (
                submission["tenant_id"] != user.tenant_id
                or submission["assignment_id"] != assignment_id
                or not bool(set(submission["class_ids"]) & class_ids)
            ):
                continue
            if user.role == "teacher" and not bool(set(user.class_ids) & set(submission["class_ids"])):
                continue
            student = self.user_by_id(submission["student_id"])
            student_rows.append(
                {
                    "student_id": submission["student_id"],
                    "student_name": student.display_name if student else submission["student_id"],
                    "submission_id": submission["submission_id"],
                    "status": submission["status"],
                    "submitted_at": submission["submitted_at"],
                    "results": [
                        {
                            "sequence": result["sequence"],
                            "problem_id": result["problem_id"],
                            "problem_text": result["problem_text"],
                            "student_answer": result["student_answer"],
                            "is_correct": result["is_correct"],
                            "error_type": result.get("error_type"),
                            "hint_level": result.get("hint_level", 0),
                            "attempt_number": result.get("attempt_number", 1),
                            "confidence_score": result.get("confidence_score", 0),
                            "routed_to_human": result.get("routed_to_human", False),
                        }
                        for result in submission["results"]
                    ],
                }
            )
        return {
            "assignment_id": assignment_id,
            "title": assignment["title"],
            "problem_stats": stats["problem_stats"],
            "student_rows": student_rows,
        }

    @staticmethod
    def _as_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed

    @staticmethod
    def _error_distribution(results: list[JsonDict]) -> dict[str, int]:
        distribution: dict[str, int] = {}
        for result in results:
            error_type = result.get("error_type")
            if error_type:
                distribution[str(error_type)] = distribution.get(str(error_type), 0) + 1
        return distribution

    def _weak_point_counts(self, submissions: list[JsonDict]) -> dict[str, JsonDict]:
        counts: dict[str, JsonDict] = {}
        for submission in submissions:
            submitted_at = submission["submitted_at"]
            for result in submission["results"]:
                if result.get("is_correct") is not False:
                    continue
                problem = self.problems.get(result["problem_id"], {})
                tags = problem.get("tags") or [result.get("error_type") or "unknown"]
                for tag in tags:
                    current = counts.setdefault(str(tag), {"count": 0, "last_error_at": submitted_at})
                    current["count"] += 1
                    if str(submitted_at) > str(current["last_error_at"]):
                        current["last_error_at"] = submitted_at
        return counts

    def _knowledge_point_alerts(self, submissions: list[JsonDict]) -> list[JsonDict]:
        totals: dict[str, int] = {}
        wrong: dict[str, set[str]] = {}
        for submission in submissions:
            for result in submission["results"]:
                problem = self.problems.get(result["problem_id"], {})
                for tag in problem.get("tags") or []:
                    totals[str(tag)] = totals.get(str(tag), 0) + 1
                    if result.get("is_correct") is False:
                        wrong.setdefault(str(tag), set()).add(submission["student_id"])
        alerts: list[JsonDict] = []
        for tag, affected_students in wrong.items():
            total = totals.get(tag, 0)
            error_rate = round(len(affected_students) / total, 3) if total else 0.0
            if error_rate >= 0.4:
                alerts.append(
                    {
                        "knowledge_point": tag,
                        "error_rate": error_rate,
                        "alert_level": "high",
                        "alert": "超过40%学生在此知识点出错，建议重点讲解",
                        "affected_student_count": len(affected_students),
                    }
                )
        alerts.sort(key=lambda item: float(item["error_rate"]), reverse=True)
        return alerts[:5]

    def _students_needing_attention(self, submissions: list[JsonDict]) -> list[JsonDict]:
        by_student: dict[str, list[JsonDict]] = {}
        for submission in submissions:
            by_student.setdefault(str(submission["student_id"]), []).extend(submission["results"])
        students: list[JsonDict] = []
        for student_id, results in by_student.items():
            if not results:
                continue
            correct = sum(result.get("is_correct") is True for result in results)
            recent_accuracy = round(correct / len(results), 3)
            hint_dependency_rate = round(
                sum(int(result.get("hint_level", 0)) > 0 for result in results) / len(results), 3
            )
            weak_points = [
                error_type
                for error_type, _count in sorted(
                    self._error_distribution(results).items(), key=lambda item: item[1], reverse=True
                )[:3]
            ]
            if recent_accuracy < 0.6 or hint_dependency_rate >= 0.5:
                student = self.user_by_id(student_id)
                students.append(
                    {
                        "student_id": student_id,
                        "student_name": student.display_name if student else student_id,
                        "recent_accuracy": recent_accuracy,
                        "weak_points": weak_points,
                        "hint_dependency_rate": hint_dependency_rate,
                        "consecutive_wrong_count": sum(result.get("is_correct") is False for result in results),
                    }
                )
        students.sort(key=lambda item: (float(item["recent_accuracy"]), -float(item["hint_dependency_rate"])))
        return students[:10]

    def _accuracy_trend(self, submissions: list[JsonDict], *, by_day: bool = False) -> list[JsonDict]:
        buckets: dict[str, list[JsonDict]] = {}
        for submission in submissions:
            submitted_at = self._as_datetime(submission["submitted_at"])
            bucket = submitted_at.date().isoformat() if by_day else submitted_at.date().isoformat()
            buckets.setdefault(bucket, []).extend(submission["results"])
        trend = []
        for bucket, results in sorted(buckets.items()):
            correct = sum(result.get("is_correct") is True for result in results)
            item: JsonDict = {
                "accuracy": round(correct / len(results), 3) if results else 0.0,
                "problems_count": len(results),
            }
            item["date" if by_day else "week"] = bucket
            trend.append(item)
        return trend

    async def reset_user_password(self, user: User, target_user_id: str, password_hash: bytes) -> JsonDict:
        from app.core.errors import AppError

        target = self.user_by_id(target_user_id)
        if target is None or target.tenant_id != user.tenant_id:
            raise AppError(404, 4004, "用户不存在")
        target.password_hash = password_hash
        target.token_version += 1
        target.force_change_password = True
        return {
            "user_id": target.user_id,
            "username": target.username,
            "display_name": target.display_name,
            "force_change_on_next_login": True,
        }

    async def update_user_status(self, user: User, target_user_id: str, is_active: bool) -> JsonDict:
        from app.core.errors import AppError

        target = self.user_by_id(target_user_id)
        if target is None or target.tenant_id != user.tenant_id:
            raise AppError(404, 4004, "用户不存在")
        if target.user_id == user.user_id and not is_active:
            raise AppError(409, 4005, "不能停用当前登录账户")
        if is_active:
            self.disabled_user_ids.discard(target.user_id)
        else:
            self.disabled_user_ids.add(target.user_id)
        target.token_version += 1
        return {
            "user_id": target.user_id,
            "username": target.username,
            "display_name": target.display_name,
            "role": target.role,
            "is_active": is_active,
        }

    async def run_harness(self, user: User, payload: dict[str, Any], report: dict[str, Any]) -> JsonDict:
        run_id = str(uuid4())
        metrics = report["metrics"]
        run = {
            "run_id": run_id,
            "status": "completed",
            "passed": not report["failures"],
            "prompt_version": "local",
            "use_mock": payload["use_mock"],
            "total_cases": metrics["total"],
            "passed_cases": metrics["total"] - len(report["failures"]),
            "accuracy": metrics["accuracy"],
            "false_positive_rate": metrics["false_positive_rate"],
            "false_negative_rate": metrics["false_negative_rate"],
            "error_cls_accuracy": None,
            "calibration_error": None,
            "coverage_matrix": {},
            "failed_cases": report["failures"],
            "run_at": utcnow().isoformat(),
            "duration_seconds": 0,
            "triggered_by_user_id": user.user_id,
        }
        self.harness_runs[run_id] = run
        return {
            "run_id": run_id,
            "status": run["status"],
            "estimated_seconds": 0,
            "use_mock": run["use_mock"],
            "total_cases": run["total_cases"],
        }

    async def harness_run_detail(self, user: User, run_id: str) -> JsonDict:
        from app.core.errors import AppError

        run = self.harness_runs.get(run_id)
        if run is None:
            raise AppError(404, 4004, "Harness 运行记录不存在")
        return run

    async def create_rag_ingest_job(
        self, user: User, payload: dict[str, Any], rag_indexer: Any | None = None
    ) -> JsonDict:
        job_id = str(uuid4())
        now = utcnow().isoformat()
        matched = [
            item
            for item in self.problems.values()
            if item["tenant_id"] == user.tenant_id
            and (not payload["grade_levels"] or item["grade_level"] in payload["grade_levels"])
        ]
        to_ingest = [
            item for item in matched if payload["force_reingest"] or item.get("embedding_status", "pending") != "done"
        ]
        for item in to_ingest:
            item["embedding_id"] = f"rag-{item['problem_id']}"
            item["embedding_status"] = "done"
        qdrant_status = "local_metadata_indexed"
        ingested_count = len(to_ingest)
        if rag_indexer is not None:
            ingested_count = await rag_indexer.upsert_problems(user.tenant_id, to_ingest)
            qdrant_status = "qdrant_indexed"
        job = {
            "job_id": job_id,
            "job_type": "rag_ingest",
            "status": "succeeded",
            "progress": 1.0,
            "created_at": now,
            "updated_at": now,
            "created_by": user.user_id,
            "payload": payload,
            "result": {
                "source": payload["source"],
                "matched_problem_count": len(matched),
                "ingested_count": ingested_count,
                "qdrant_status": qdrant_status,
            },
            "error_message": None,
        }
        self.jobs[job_id] = job
        return {
            "job_id": job_id,
            "status": "succeeded",
            "matched_problem_count": len(matched),
            "ingested_count": ingested_count,
        }

    async def job_detail(self, user: User, job_id: str) -> JsonDict:
        from app.core.errors import AppError

        job = self.jobs.get(job_id)
        if job is None or (user.role == "admin" and job["created_by"] != user.user_id):
            raise AppError(404, 4004, "后台任务不存在")
        return job
