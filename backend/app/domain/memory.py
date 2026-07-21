from __future__ import annotations

from copy import deepcopy

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
        self.reset()

    def reset(self) -> None:
        self.users: dict[str, User] = deepcopy(self._seed_users)
        self.problems: dict[str, JsonDict] = {}
        self.assignments: dict[str, JsonDict] = {}
        self.submissions: dict[str, JsonDict] = {}
        self.reviews: dict[str, JsonDict] = {}
        self.tickets: dict[str, Ticket] = {}
        self.events: dict[str, list[JsonDict]] = {}

    def user_by_id(self, user_id: str) -> User | None:
        return next((user for user in self.users.values() if user.user_id == user_id), None)

    def known_class_ids(self, tenant_id: str) -> set[str]:
        return {class_id for user in self.users.values() if user.tenant_id == tenant_id for class_id in user.class_ids}

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
