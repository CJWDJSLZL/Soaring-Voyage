from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
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
        return self.users.get(username)

    async def identity_by_id(self, user_id: str, tenant_id: str, role: str) -> User | None:
        user = self.user_by_id(user_id)
        if user is None or user.tenant_id != tenant_id or user.role != role:
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

    async def increment_token_version(self, user: User) -> None:
        user.token_version += 1

    async def create_catalog_problem(self, user: User, problem: dict[str, Any]) -> str:
        problem_id = str(uuid4())
        self.problems[problem_id] = {
            "problem_id": problem_id,
            "tenant_id": user.tenant_id,
            "created_by": user.user_id,
            **problem,
            "created_at": utcnow().isoformat(),
        }
        return problem_id

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
        items = [item for item in self.problems.values() if item.get("tenant_id") == user.tenant_id]
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
