from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.db.session import tenant_conn, tenant_context
from app.domain.models import JsonDict, User

NIL_SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"

_USER_COLUMNS = """
    u.id, u.username, u.display_name, u.password_hash, u.role, u.tenant_id,
    u.grade_level, u.login_fail_count, u.locked_until, u.token_version,
    CASE
      WHEN u.role = 'teacher' THEN ARRAY(
        SELECT c.id FROM classes c
        WHERE c.tenant_id = u.tenant_id AND c.teacher_id = u.id AND NOT c.is_deleted
        ORDER BY c.id
      )
      WHEN u.role = 'student' THEN ARRAY(
        SELECT cs.class_id FROM class_students cs
        WHERE cs.tenant_id = u.tenant_id AND cs.student_id = u.id AND cs.is_active
        ORDER BY cs.class_id
      )
      ELSE ARRAY[]::uuid[]
    END AS class_ids
"""
_USER_BY_USERNAME_SQL = (
    "SELECT "  # nosec B608  # noqa: S608 -- fixed module SQL fragments only
    + _USER_COLUMNS
    + " FROM users u WHERE u.tenant_id = $1 AND u.username = $2 AND NOT u.is_deleted"
)
_USER_BY_ID_SQL = (
    "SELECT "  # nosec B608  # noqa: S608 -- fixed module SQL fragments only
    + _USER_COLUMNS
    + " FROM users u WHERE u.tenant_id = $1 AND u.id = $2 AND NOT u.is_deleted"
)


class PostgresIdentityProblemRepository:
    """asyncpg adapter for identity and problem catalog persistence.

    Every operation enters a transaction-local tenant context. Authentication
    lookups use the configured single-school tenant and a nil system user; no
    connection or role is ever configured to bypass RLS globally.
    """

    def __init__(self, pool: asyncpg.Pool, default_tenant_id: str) -> None:
        self.pool = pool
        self.default_tenant_id = str(UUID(default_tenant_id))

    @staticmethod
    def user_from_row(row: Mapping[str, Any]) -> User:
        stored_hash = row["password_hash"] or ""
        password_hash = stored_hash.encode("utf-8") if isinstance(stored_hash, str) else bytes(stored_hash)
        return User(
            user_id=str(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"] or row["username"]),
            password_hash=password_hash,
            role=str(row["role"]),
            tenant_id=str(row["tenant_id"]),
            class_ids=[str(value) for value in (row["class_ids"] or [])],
            grade_level=row["grade_level"],
            failed_logins=int(row["login_fail_count"]),
            locked_until=row["locked_until"],
            token_version=int(row["token_version"]),
        )

    @asynccontextmanager
    async def _connection(self, tenant_id: str, role: str, user_id: str) -> AsyncIterator[asyncpg.Connection]:
        with tenant_context(tenant_id, role):
            async with tenant_conn(self.pool, user_id=user_id) as connection:
                yield connection

    def _preauth_connection(self):
        return self._connection(self.default_tenant_id, "worker", NIL_SYSTEM_USER_ID)

    async def identity_by_username(self, username: str) -> User | None:
        async with self._preauth_connection() as connection:
            row = await connection.fetchrow(_USER_BY_USERNAME_SQL, self.default_tenant_id, username)
        return self.user_from_row(row) if row else None

    async def identity_by_id(self, user_id: str, tenant_id: str, role: str) -> User | None:
        async with self._connection(tenant_id, role, user_id) as connection:
            row = await connection.fetchrow(_USER_BY_ID_SQL, tenant_id, user_id)
        return self.user_from_row(row) if row else None

    async def register_login_failure(self, user: User, *, max_failures: int, locked_until: datetime) -> User:
        async with self._preauth_connection() as connection:
            row = await connection.fetchrow(
                """
                UPDATE users
                SET login_fail_count = login_fail_count + 1,
                    locked_until = CASE
                        WHEN login_fail_count + 1 >= $3 THEN $4
                        ELSE locked_until
                    END
                WHERE tenant_id = $1 AND id = $2 AND NOT is_deleted
                RETURNING login_fail_count, locked_until
                """,
                user.tenant_id,
                user.user_id,
                max_failures,
                locked_until,
            )
        if row is None:
            return user
        user.failed_logins = int(row["login_fail_count"])
        user.locked_until = row["locked_until"]
        return user

    async def clear_login_failures(self, user: User) -> None:
        async with self._preauth_connection() as connection:
            await connection.execute(
                """
                UPDATE users SET login_fail_count = 0, locked_until = NULL, last_login_at = now()
                WHERE tenant_id = $1 AND id = $2 AND NOT is_deleted
                """,
                user.tenant_id,
                user.user_id,
            )
        user.failed_logins = 0
        user.locked_until = None

    async def replace_password(self, user: User, password_hash: bytes) -> None:
        encoded = password_hash.decode("utf-8")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            version = await connection.fetchval(
                """
                UPDATE users
                SET password_hash = $3, token_version = token_version + 1
                WHERE tenant_id = $1 AND id = $2 AND NOT is_deleted
                RETURNING token_version
                """,
                user.tenant_id,
                user.user_id,
                encoded,
            )
        user.password_hash = password_hash
        user.token_version = int(version)

    async def increment_token_version(self, user: User) -> None:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            version = await connection.fetchval(
                """
                UPDATE users SET token_version = token_version + 1
                WHERE tenant_id = $1 AND id = $2 AND NOT is_deleted
                RETURNING token_version
                """,
                user.tenant_id,
                user.user_id,
            )
        user.token_version = int(version)

    async def create_catalog_problem(self, user: User, problem: dict[str, Any]) -> str:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            problem_id = await connection.fetchval(
                """
                INSERT INTO problems (
                    tenant_id, created_by, problem_type, grade_level, difficulty,
                    curriculum_version, problem_text, reference_answer,
                    solution_steps, common_errors, tags
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11)
                RETURNING id
                """,
                user.tenant_id,
                user.user_id,
                problem["problem_type"],
                problem["grade_level"],
                problem["difficulty"],
                problem["curriculum_version"],
                problem["problem_text"],
                problem["reference_answer"],
                json.dumps(problem.get("solution_steps", []), ensure_ascii=False),
                json.dumps(problem.get("common_errors", []), ensure_ascii=False),
                problem.get("tags", []),
            )
        return str(problem_id)

    @staticmethod
    def _json_value(value: Any, default: Any) -> Any:
        if value is None:
            return default
        return json.loads(value) if isinstance(value, str) else value

    @classmethod
    def problem_from_row(cls, row: Mapping[str, Any]) -> JsonDict:
        return {
            "problem_id": str(row["id"]),
            "tenant_id": str(row["tenant_id"]),
            "created_by": str(row["created_by"]),
            "problem_type": row["problem_type"],
            "grade_level": row["grade_level"],
            "difficulty": row["difficulty"],
            "curriculum_version": row["curriculum_version"],
            "problem_text": row["problem_text"],
            "reference_answer": row["reference_answer"],
            "solution_steps": cls._json_value(row["solution_steps"], []),
            "common_errors": cls._json_value(row["common_errors"], []),
            "tags": list(row["tags"] or []),
            "created_at": row["created_at"].isoformat(),
        }

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
        where = ["tenant_id = $1", "NOT is_deleted"]
        arguments: list[Any] = [user.tenant_id]
        for column, value in (
            ("grade_level", grade_level),
            ("problem_type", problem_type),
            ("difficulty", difficulty),
        ):
            if value is not None:
                arguments.append(value)
                where.append(f"{column} = ${len(arguments)}")
        if keyword is not None:
            arguments.append(f"%{keyword}%")
            where.append(f"problem_text ILIKE ${len(arguments)}")
        predicate = " AND ".join(where)
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            # The predicate contains only fixed column names above; all caller
            # values remain asyncpg parameters.
            total = await connection.fetchval(
                f"SELECT count(*) FROM problems WHERE {predicate}",  # nosec B608  # noqa: S608
                *arguments,
            )
            query_arguments = [*arguments, page_size, (page_number - 1) * page_size]
            rows = await connection.fetch(
                f"""
                SELECT id, tenant_id, created_by, problem_type, grade_level, difficulty,
                       curriculum_version, problem_text, reference_answer, solution_steps,
                       common_errors, tags, created_at
                FROM problems
                WHERE {predicate}
                ORDER BY created_at DESC, id DESC
                LIMIT ${len(arguments) + 1} OFFSET ${len(arguments) + 2}
                """,  # nosec B608  # noqa: S608 -- internal allowlisted SQL fragments
                *query_arguments,
            )
        count = int(total or 0)
        start = (page_number - 1) * page_size
        return {
            "items": [self.problem_from_row(row) for row in rows],
            "total": count,
            "page": page_number,
            "page_size": page_size,
            "has_next": start + page_size < count,
        }
