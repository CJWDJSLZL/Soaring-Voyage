from __future__ import annotations

from copy import deepcopy
from datetime import datetime
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
        total = len(items)
        start = (page_number - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "total": total,
            "page": page_number,
            "page_size": page_size,
            "has_next": start + page_size < total,
        }
