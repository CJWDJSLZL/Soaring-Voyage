from __future__ import annotations

from typing import Protocol

from .models import JsonDict, Ticket, User


class Repository(Protocol):
    users: dict[str, User]
    problems: dict[str, JsonDict]
    assignments: dict[str, JsonDict]
    submissions: dict[str, JsonDict]
    reviews: dict[str, JsonDict]
    tickets: dict[str, Ticket]
    events: dict[str, list[JsonDict]]

    def reset(self) -> None: ...
    def user_by_id(self, user_id: str) -> User | None: ...
    def known_class_ids(self, tenant_id: str) -> set[str]: ...
    def purge_expired_tickets(self) -> int: ...
    def submission_for(self, student_id: str, assignment_id: str) -> JsonDict | None: ...
