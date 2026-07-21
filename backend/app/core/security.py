from __future__ import annotations

from datetime import timedelta

import bcrypt
import jwt

from app.config import settings
from app.domain.models import User

from .errors import utcnow


def hash_password(password: str) -> bytes:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.bcrypt_rounds))


def verify_password(password: str, password_hash: bytes) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash)


def create_access_token(user: User) -> str:
    now = utcnow()
    payload = {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "username": user.username,
        "grade_level": user.grade_level,
        "token_version": user.token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_expires_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
