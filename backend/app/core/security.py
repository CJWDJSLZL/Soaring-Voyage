from __future__ import annotations

from datetime import timedelta

import bcrypt
import jwt

from app.config import Settings, settings
from app.domain.models import User

from .errors import utcnow


def hash_password(password: str, configured: Settings = settings) -> bytes:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=configured.bcrypt_rounds))


def verify_password(password: str, password_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash)
    except ValueError:
        # Imported/SSO-only users may not have a local bcrypt hash. Treat an
        # absent or malformed hash as invalid credentials, never as a 500.
        return False


def create_access_token(user: User, configured: Settings = settings) -> str:
    now = utcnow()
    payload = {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "username": user.username,
        "grade_level": user.grade_level,
        "token_version": user.token_version,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=configured.jwt_expires_seconds)).timestamp()),
    }
    return jwt.encode(payload, configured.jwt_secret, algorithm=configured.jwt_algorithm)


def decode_access_token(token: str, configured: Settings = settings) -> dict:
    return jwt.decode(token, configured.jwt_secret, algorithms=[configured.jwt_algorithm])
