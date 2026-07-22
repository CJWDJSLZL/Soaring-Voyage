from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from .models import JsonDict, Ticket, User


class Repository(Protocol):
    """Legacy in-memory boundary for workflows not yet ported to PostgreSQL."""

    users: dict[str, User]
    classes: dict[str, JsonDict]
    problems: dict[str, JsonDict]
    assignments: dict[str, JsonDict]
    submissions: dict[str, JsonDict]
    attempts: dict[str, dict[str, list[JsonDict]]]
    reviews: dict[str, JsonDict]
    knowledge_records: dict[str, JsonDict]
    tickets: dict[str, Ticket]
    events: dict[str, list[JsonDict]]

    def reset(self) -> None: ...
    def user_by_id(self, user_id: str) -> User | None: ...
    def known_class_ids(self, tenant_id: str) -> set[str]: ...
    def class_name(self, tenant_id: str, class_id: str) -> str: ...
    def purge_expired_tickets(self) -> int: ...
    def submission_for(self, student_id: str, assignment_id: str) -> JsonDict | None: ...


class IdentityProblemRepository(Protocol):
    """Async boundary for migrated PostgreSQL-backed workflows."""

    async def identity_by_username(self, username: str) -> User | None: ...
    async def identity_by_id(self, user_id: str, tenant_id: str, role: str) -> User | None: ...
    async def register_login_failure(self, user: User, *, max_failures: int, locked_until: datetime) -> User: ...
    async def clear_login_failures(self, user: User) -> None: ...
    async def replace_password(self, user: User, password_hash: bytes) -> None: ...
    async def increment_token_version(self, user: User) -> None: ...
    async def create_catalog_problem(self, user: User, problem: dict[str, Any]) -> str: ...
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
    ) -> JsonDict: ...
    async def create_assignment(self, user: User, payload: dict[str, Any]) -> JsonDict: ...
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
    ) -> JsonDict: ...
    async def assignment_detail(self, user: User, assignment_id: str) -> JsonDict: ...
