from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Request


class AppError(Exception):
    def __init__(
        self,
        status_code: int,
        code: int,
        message: str,
        detail: Any | None = None,
        **extra: Any,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = detail
        self.extra = extra


def trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "req-unknown")


def envelope(request: Request, data: Any = None, message: str = "success") -> dict[str, Any]:
    return {"code": 0, "message": message, "data": data, "trace_id": trace_id(request)}


def utcnow() -> datetime:
    return datetime.now(UTC)
