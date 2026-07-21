from __future__ import annotations

from typing import Protocol

from .models import JsonDict, Ticket, User


class Repository(Protocol):
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
