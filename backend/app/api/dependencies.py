from __future__ import annotations

from collections.abc import Callable

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.errors import AppError
from app.core.security import decode_access_token
from app.domain.models import User
from app.domain.repository import IdentityProblemRepository, Repository
from app.grading import DeepSeekGradingClient

bearer = HTTPBearer(auto_error=False)


def get_store(request: Request) -> Repository:
    return request.app.state.store


def get_identity_repository(request: Request) -> IdentityProblemRepository:
    return request.app.state.identity_repository


def get_llm_grader(request: Request) -> DeepSeekGradingClient:
    return request.app.state.llm_grader


async def current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
) -> User:
    if credentials is None:
        raise AppError(401, 4002, "未登录")
    try:
        claims = decode_access_token(credentials.credentials, request.app.state.settings)
    except jwt.PyJWTError as exc:
        raise AppError(401, 4001, "Token 无效或过期") from exc
    user = await repository.identity_by_id(
        claims.get("user_id", ""), claims.get("tenant_id", ""), claims.get("role", "")
    )
    if (
        user is None
        or user.tenant_id != claims.get("tenant_id")
        or user.role != claims.get("role")
        or user.token_version != claims.get("token_version")
    ):
        raise AppError(401, 4001, "Token 无效或过期")
    return user


def require_roles(*roles: str) -> Callable:
    def dependency(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise AppError(403, 4003, "权限不足")
        return user

    return dependency
