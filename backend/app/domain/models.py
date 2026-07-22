from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class User:
    user_id: str
    username: str
    display_name: str
    password_hash: bytes
    role: str
    tenant_id: str
    class_ids: list[str] = field(default_factory=list)
    grade_level: int | None = None
    failed_logins: int = 0
    locked_until: datetime | None = None
    token_version: int = 0
    force_change_password: bool = False


@dataclass
class Ticket:
    user_id: str
    tenant_id: str
    submission_id: str
    role: str
    expires_at: datetime


JsonDict = dict[str, Any]
