from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg

from app.core.errors import AppError
from app.db.session import tenant_conn, tenant_context
from app.domain.models import JsonDict, Ticket, User

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

    @staticmethod
    def _page(items: list[JsonDict], page_number: int, page_size: int) -> JsonDict:
        total = len(items)
        start = (page_number - 1) * page_size
        return {
            "items": items[start : start + page_size],
            "total": total,
            "page": page_number,
            "page_size": page_size,
            "has_next": start + page_size < total,
        }

    @staticmethod
    def _assignment_status(due_date: datetime | None) -> str:
        if due_date is None:
            return "active"
        due = due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)
        return "expired" if due <= datetime.now(UTC) else "active"

    @staticmethod
    def _public_submission(submission: JsonDict) -> JsonDict:
        keys = ("submission_id", "status", "submitted_at", "results", "summary", "last_updated_at")
        public = {key: submission[key] for key in keys if key in submission}
        if "results" in public:
            public["results"] = [
                {key: value for key, value in result.items() if key != "agent_trace"} for result in public["results"]
            ]
        return public

    @staticmethod
    def _uuid_text(value: Any, field: str) -> str:
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AppError(422, 4022, "请求参数校验失败", f"{field} must be a valid UUID") from exc

    async def create_assignment(self, user: User, payload: dict[str, Any]) -> JsonDict:
        class_ids = list(payload["class_ids"])
        problem_ids = list(payload["problem_ids"])
        if len(class_ids) != len(set(class_ids)):
            raise AppError(422, 4022, "请求参数校验失败", "class_ids must be unique")
        if len(problem_ids) != len(set(problem_ids)):
            raise AppError(422, 4022, "请求参数校验失败", "problem_ids must be unique")
        if user.role == "teacher" and not set(class_ids).issubset(set(user.class_ids)):
            raise AppError(403, 4003, "教师只能向本人班级布置作业")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            class_rows = await connection.fetch(
                """
                SELECT id, name FROM classes
                WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND NOT is_deleted
                ORDER BY id
                """,
                user.tenant_id,
                class_ids,
            )
            found_classes = {str(row["id"]): row["name"] for row in class_rows}
            if set(found_classes) != set(class_ids):
                raise AppError(404, 4004, "班级不存在")
            problem_rows = await connection.fetch(
                """
                SELECT id FROM problems
                WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND NOT is_deleted
                """,
                user.tenant_id,
                problem_ids,
            )
            found_problem_ids = {str(row["id"]) for row in problem_rows}
            missing = [item for item in problem_ids if item not in found_problem_ids]
            if missing:
                raise AppError(404, 4004, "题目不存在", f"Missing problem ids: {missing}")
            assignment_id = await connection.fetchval(
                """
                INSERT INTO assignments (tenant_id, title, due_date, created_by)
                VALUES ($1, $2, $3, $4)
                RETURNING id
                """,
                user.tenant_id,
                payload["title"],
                payload.get("due_date"),
                user.user_id,
            )
            for class_id in class_ids:
                await connection.execute(
                    """
                    INSERT INTO assignment_classes (tenant_id, assignment_id, class_id)
                    VALUES ($1, $2, $3)
                    """,
                    user.tenant_id,
                    assignment_id,
                    class_id,
                )
            for position, problem_id in enumerate(problem_ids, 1):
                await connection.execute(
                    """
                    INSERT INTO assignment_problems (tenant_id, assignment_id, problem_id, position)
                    VALUES ($1, $2, $3, $4)
                    """,
                    user.tenant_id,
                    assignment_id,
                    problem_id,
                    position,
                )
            created_at = await connection.fetchval(
                "SELECT created_at FROM assignments WHERE tenant_id = $1 AND id = $2",
                user.tenant_id,
                assignment_id,
            )
        due_date = payload.get("due_date")
        return {
            "assignment_id": str(assignment_id),
            "title": payload["title"],
            "classes": [{"class_id": class_id, "class_name": str(found_classes[class_id])} for class_id in class_ids],
            "due_date": due_date.isoformat() if isinstance(due_date, datetime) else due_date,
            "problem_count": len(problem_ids),
            "created_at": created_at.isoformat(),
            "status": self._assignment_status(due_date),
        }

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
    ) -> JsonDict:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            rows = await connection.fetch(
                """
                SELECT a.id, a.title, a.due_date, a.created_at,
                       array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids,
                       string_agg(DISTINCT c.name, '、' ORDER BY c.name) AS class_name,
                       count(DISTINCT ap.problem_id) AS problem_count,
                       s.status AS submission_status
                FROM assignments a
                JOIN assignment_classes ac ON ac.tenant_id = a.tenant_id AND ac.assignment_id = a.id
                JOIN classes c ON c.tenant_id = ac.tenant_id AND c.id = ac.class_id AND NOT c.is_deleted
                JOIN assignment_problems ap ON ap.tenant_id = a.tenant_id AND ap.assignment_id = a.id
                LEFT JOIN submissions s
                  ON s.tenant_id = a.tenant_id AND s.assignment_id = a.id AND s.student_id = $2
                WHERE a.tenant_id = $1 AND NOT a.is_deleted
                  AND ($3::uuid IS NULL OR ac.class_id = $3::uuid)
                GROUP BY a.id, a.title, a.due_date, a.created_at, s.status
                """,
                user.tenant_id,
                user.user_id,
                class_id,
            )
        visible_items: list[JsonDict] = []
        user_class_ids = set(user.class_ids)
        for row in rows:
            assignment_class_ids = {str(value) for value in row["class_ids"]}
            if user.role not in {"admin", "sysadmin"} and not (user_class_ids & assignment_class_ids):
                continue
            current_status = self._assignment_status(row["due_date"])
            if status != "all" and current_status != status:
                continue
            due = row["due_date"].isoformat() if row["due_date"] else None
            item = {
                "assignment_id": str(row["id"]),
                "title": row["title"],
                "class_name": row["class_name"],
                "due_date": due,
                "problem_count": int(row["problem_count"]),
                "status": current_status,
                "is_expiring_soon": False,
                "created_at": row["created_at"].isoformat(),
            }
            if user.role == "student":
                item["submission_status"] = row["submission_status"] or "not_submitted"
            visible_items.append(item)
        reverse = order == "desc"
        visible_items.sort(key=lambda item: str(item.get(order_by) or ""), reverse=reverse)
        return self._page(visible_items, page_number, page_size)

    async def assignment_detail(self, user: User, assignment_id: str) -> JsonDict:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            assignment = await connection.fetchrow(
                """
                SELECT a.id, a.title, a.due_date,
                       array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids,
                       string_agg(DISTINCT c.name, '、' ORDER BY c.name) AS class_name,
                       s.id AS submission_id, s.status AS submission_status
                FROM assignments a
                JOIN assignment_classes ac ON ac.tenant_id = a.tenant_id AND ac.assignment_id = a.id
                JOIN classes c ON c.tenant_id = ac.tenant_id AND c.id = ac.class_id AND NOT c.is_deleted
                LEFT JOIN submissions s
                  ON s.tenant_id = a.tenant_id AND s.assignment_id = a.id AND s.student_id = $3
                WHERE a.tenant_id = $1 AND a.id = $2 AND NOT a.is_deleted
                GROUP BY a.id, a.title, a.due_date, s.id, s.status
                """,
                user.tenant_id,
                assignment_id,
                user.user_id,
            )
            if assignment is None:
                raise AppError(404, 4004, "作业不存在")
            assignment_class_ids = {str(value) for value in assignment["class_ids"]}
            if user.role not in {"admin", "sysadmin"} and not (set(user.class_ids) & assignment_class_ids):
                raise AppError(404, 4004, "作业不存在")
            rows = await connection.fetch(
                """
                SELECT p.id, p.problem_text, p.problem_type, p.grade_level, p.difficulty, p.tags,
                       ap.position
                FROM assignment_problems ap
                JOIN problems p ON p.id = ap.problem_id AND p.tenant_id = ap.tenant_id AND NOT p.is_deleted
                WHERE ap.tenant_id = $1 AND ap.assignment_id = $2
                ORDER BY ap.position
                """,
                user.tenant_id,
                assignment_id,
            )
        fields = ["problem_id", "problem_text", "problem_type", "grade_level", "difficulty", "tags"]
        if user.role == "student":
            fields = ["problem_id", "problem_text", "problem_type", "difficulty"]
        problems = []
        for row in rows:
            problem = {
                "problem_id": str(row["id"]),
                "problem_text": row["problem_text"],
                "problem_type": row["problem_type"],
                "grade_level": row["grade_level"],
                "difficulty": row["difficulty"],
                "tags": list(row["tags"] or []),
            }
            problems.append({"sequence": row["position"], **{key: problem[key] for key in fields}})
        data = {
            "assignment_id": assignment_id,
            "title": assignment["title"],
            "class_name": assignment["class_name"],
            "due_date": assignment["due_date"].isoformat() if assignment["due_date"] else None,
            "status": self._assignment_status(assignment["due_date"]),
            "problems": problems,
        }
        if user.role == "student":
            data["my_submission"] = (
                {"submission_id": str(assignment["submission_id"]), "status": assignment["submission_status"]}
                if assignment["submission_id"]
                else None
            )
        return data

    async def patch_assignment(self, user: User, assignment_id: str, payload: dict[str, Any]) -> JsonDict:
        normalized_assignment_id = self._uuid_text(assignment_id, "assignment_id")
        add_problem_ids = list(payload.get("add_problem_ids") or [])
        remove_problem_ids = list(payload.get("remove_problem_ids") or [])
        if (
            len(add_problem_ids) != len(set(add_problem_ids))
            or len(remove_problem_ids) != len(set(remove_problem_ids))
            or set(add_problem_ids) & set(remove_problem_ids)
        ):
            raise AppError(422, 4022, "请求参数校验失败", "problem patch ids must be unique and disjoint")
        add_problem_ids = [self._uuid_text(value, "problem_id") for value in add_problem_ids]
        remove_problem_ids = [self._uuid_text(value, "problem_id") for value in remove_problem_ids]
        class_ids = payload.get("class_ids")
        if class_ids is not None:
            class_ids = [self._uuid_text(value, "class_id") for value in class_ids]
            if len(class_ids) != len(set(class_ids)):
                raise AppError(422, 4022, "请求参数校验失败", "class_ids must be unique")
            if user.role == "teacher" and not set(class_ids).issubset(set(user.class_ids)):
                raise AppError(403, 4003, "教师只能向本人班级布置作业")

        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            assignment = await connection.fetchrow(
                """
                SELECT a.id, a.title, a.due_date,
                       array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids
                FROM assignments a
                JOIN assignment_classes ac ON ac.tenant_id = a.tenant_id AND ac.assignment_id = a.id
                WHERE a.tenant_id = $1 AND a.id = $2 AND NOT a.is_deleted
                GROUP BY a.id, a.title, a.due_date
                """,
                user.tenant_id,
                normalized_assignment_id,
            )
            if assignment is None:
                raise AppError(404, 4004, "作业不存在")
            existing_class_ids = {str(value) for value in assignment["class_ids"]}
            if user.role == "teacher" and not existing_class_ids.issubset(set(user.class_ids)):
                raise AppError(403, 4003, "教师只能修改完全属于本人班级的作业")
            if user.role not in {"admin", "sysadmin", "teacher"}:
                raise AppError(403, 4003, "权限不足")
            if self._assignment_status(assignment["due_date"]) == "expired":
                raise AppError(409, 4005, "作业已截止，不可修改")

            current_rows = await connection.fetch(
                """
                SELECT problem_id
                FROM assignment_problems
                WHERE tenant_id = $1 AND assignment_id = $2
                ORDER BY position
                """,
                user.tenant_id,
                normalized_assignment_id,
            )
            current_problem_ids = [str(row["problem_id"]) for row in current_rows]
            all_problem_ids = add_problem_ids + remove_problem_ids
            if all_problem_ids:
                problem_rows = await connection.fetch(
                    """
                    SELECT id FROM problems
                    WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND NOT is_deleted
                    """,
                    user.tenant_id,
                    all_problem_ids,
                )
                found_problem_ids = {str(row["id"]) for row in problem_rows}
                if set(all_problem_ids) != found_problem_ids:
                    raise AppError(404, 4004, "题目不存在")

            resulting_ids = [problem_id for problem_id in current_problem_ids if problem_id not in remove_problem_ids]
            resulting_ids.extend(problem_id for problem_id in add_problem_ids if problem_id not in resulting_ids)
            if not resulting_ids:
                raise AppError(422, 4022, "请求参数校验失败", "assignment must contain at least one problem")
            if len(resulting_ids) > 50:
                raise AppError(422, 4022, "请求参数校验失败", "assignment cannot contain more than 50 problems")
            if remove_problem_ids:
                answered = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM submission_answers
                    WHERE tenant_id = $1 AND submission_id IN (
                        SELECT id FROM submissions WHERE tenant_id = $1 AND assignment_id = $2
                    ) AND problem_id = ANY($3::uuid[])
                    """,
                    user.tenant_id,
                    normalized_assignment_id,
                    remove_problem_ids,
                )
                if int(answered or 0):
                    raise AppError(409, 4005, "该题目已有学生提交，不可移除")

            if class_ids is not None:
                class_rows = await connection.fetch(
                    """
                    SELECT id FROM classes
                    WHERE tenant_id = $1 AND id = ANY($2::uuid[]) AND NOT is_deleted
                    """,
                    user.tenant_id,
                    class_ids,
                )
                if {str(row["id"]) for row in class_rows} != set(class_ids):
                    raise AppError(404, 4004, "班级不存在")
                await connection.execute(
                    "DELETE FROM assignment_classes WHERE tenant_id = $1 AND assignment_id = $2",
                    user.tenant_id,
                    normalized_assignment_id,
                )
                for class_id in class_ids:
                    await connection.execute(
                        """
                        INSERT INTO assignment_classes (tenant_id, assignment_id, class_id)
                        VALUES ($1, $2, $3)
                        """,
                        user.tenant_id,
                        normalized_assignment_id,
                        class_id,
                    )

            if add_problem_ids or remove_problem_ids:
                await connection.execute(
                    "DELETE FROM assignment_problems WHERE tenant_id = $1 AND assignment_id = $2",
                    user.tenant_id,
                    normalized_assignment_id,
                )
                for position, problem_id in enumerate(resulting_ids, 1):
                    await connection.execute(
                        """
                        INSERT INTO assignment_problems (tenant_id, assignment_id, problem_id, position)
                        VALUES ($1, $2, $3, $4)
                        """,
                        user.tenant_id,
                        normalized_assignment_id,
                        problem_id,
                        position,
                    )

            title = payload.get("title", assignment["title"])
            due_date = payload["due_date"] if "due_date" in payload else assignment["due_date"]
            row = await connection.fetchrow(
                """
                UPDATE assignments
                SET title = $3, due_date = $4
                WHERE tenant_id = $1 AND id = $2
                RETURNING title, due_date
                """,
                user.tenant_id,
                normalized_assignment_id,
                title,
                due_date,
            )
        return {
            "assignment_id": normalized_assignment_id,
            "title": row["title"],
            "due_date": row["due_date"].isoformat() if row["due_date"] else None,
            "problem_count": len(resulting_ids),
        }

    async def _visible_assignment(
        self,
        connection: asyncpg.Connection,
        user: User,
        assignment_id: str,
    ) -> Mapping[str, Any]:
        assignment = await connection.fetchrow(
            """
            SELECT a.id, a.title, a.due_date,
                   array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids
            FROM assignments a
            JOIN assignment_classes ac ON ac.tenant_id = a.tenant_id AND ac.assignment_id = a.id
            WHERE a.tenant_id = $1 AND a.id = $2 AND NOT a.is_deleted
            GROUP BY a.id, a.title, a.due_date
            """,
            user.tenant_id,
            assignment_id,
        )
        if assignment is None:
            raise AppError(404, 4004, "作业不存在")
        assignment_class_ids = {str(value) for value in assignment["class_ids"]}
        if user.role not in {"admin", "sysadmin"} and not (set(user.class_ids) & assignment_class_ids):
            raise AppError(403, 4003, "该作业不属于你所在的班级")
        return assignment

    async def submit_assignment(
        self,
        user: User,
        payload: dict[str, Any],
        grade_problem: Callable[[JsonDict, str], Awaitable[JsonDict]],
    ) -> JsonDict:
        assignment_id = self._uuid_text(payload["assignment_id"], "assignment_id")
        answer_by_problem = {
            self._uuid_text(item["problem_id"], "problem_id"): item["answer_text"] for item in payload["answers"]
        }
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            assignment = await self._visible_assignment(connection, user, assignment_id)
            due_date = assignment["due_date"]
            if due_date is not None and self._assignment_status(due_date) == "expired":
                raise AppError(410, 4006, "作业已截止，无法提交")
            existing = await connection.fetchval(
                """
                SELECT id FROM submissions
                WHERE tenant_id = $1 AND student_id = $2 AND assignment_id = $3
                """,
                user.tenant_id,
                user.user_id,
                assignment_id,
            )
            if existing is not None:
                raise AppError(409, 4005, "该作业已提交，不可重复提交")
            problem_rows = await connection.fetch(
                """
                SELECT p.id, p.problem_text, p.problem_type, p.grade_level, p.difficulty,
                       p.reference_answer, p.solution_steps, p.common_errors, p.tags, ap.position
                FROM assignment_problems ap
                JOIN problems p ON p.id = ap.problem_id AND NOT p.is_deleted
                WHERE ap.tenant_id = $1 AND ap.assignment_id = $2
                ORDER BY ap.position
                """,
                user.tenant_id,
                assignment_id,
            )
            allowed_ids = {str(row["id"]) for row in problem_rows}
            if set(answer_by_problem) != allowed_ids:
                if any(problem_id not in allowed_ids for problem_id in answer_by_problem):
                    raise AppError(403, 4003, "题目不属于该作业")
                raise AppError(422, 4022, "请求参数校验失败", "answers must cover every assignment problem")

            submission_row = await connection.fetchrow(
                """
                INSERT INTO submissions (tenant_id, assignment_id, student_id, status)
                VALUES ($1, $2, $3, 'grading')
                RETURNING id, submitted_at, updated_at
                """,
                user.tenant_id,
                assignment_id,
                user.user_id,
            )
            submission_id = str(submission_row["id"])
            results: list[JsonDict] = []
            pending = 0
            for row in problem_rows:
                problem_id = str(row["id"])
                problem = {
                    "problem_id": problem_id,
                    "problem_text": row["problem_text"],
                    "problem_type": row["problem_type"],
                    "grade_level": row["grade_level"],
                    "difficulty": row["difficulty"],
                    "reference_answer": row["reference_answer"],
                    "solution_steps": self._json_value(row["solution_steps"], []),
                    "common_errors": self._json_value(row["common_errors"], []),
                    "tags": list(row["tags"] or []),
                }
                graded = await grade_problem(problem, str(answer_by_problem[problem_id]))
                result = {
                    "problem_id": problem_id,
                    "sequence": int(row["position"]),
                    "problem_text": row["problem_text"],
                    **graded,
                }
                await connection.execute(
                    """
                    INSERT INTO submission_answers (
                        tenant_id, submission_id, problem_id, answer_text, hint_level, attempt_number
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    user.tenant_id,
                    submission_id,
                    problem_id,
                    result["student_answer"],
                    result["hint_level"],
                    result["attempt_number"],
                )
                grading_id = await connection.fetchval(
                    """
                    INSERT INTO grading_results (
                        tenant_id, submission_id, problem_id, attempt_number, is_correct,
                        confidence_score, error_type, feedback_text, encouragement, next_hint,
                        routed_to_human, human_review_reason, source, agent_trace
                    )
                    VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                        $11, $12, $13, $14::jsonb
                    )
                    RETURNING id
                    """,
                    user.tenant_id,
                    submission_id,
                    problem_id,
                    result["attempt_number"],
                    result["is_correct"],
                    result["confidence_score"],
                    result["error_type"],
                    result["feedback_text"],
                    result["encouragement"],
                    result["next_hint"],
                    result["routed_to_human"],
                    "low_confidence" if result["routed_to_human"] else None,
                    result["grading_source"],
                    json.dumps(result.get("agent_trace", []), ensure_ascii=False),
                )
                if result["routed_to_human"]:
                    pending += 1
                    await connection.execute(
                        """
                        INSERT INTO human_review_queue (tenant_id, grading_result_id, reason)
                        VALUES ($1, $2, 'low_confidence')
                        """,
                        user.tenant_id,
                        grading_id,
                    )
                results.append(result)

            correct = sum(result["is_correct"] is True for result in results)
            status = "partial_human_review" if pending else "graded"
            updated_at = await connection.fetchval(
                """
                UPDATE submissions SET status = $3
                WHERE tenant_id = $1 AND id = $2
                RETURNING updated_at
                """,
                user.tenant_id,
                submission_id,
                status,
            )

        submission = {
            "submission_id": submission_id,
            "status": status,
            "submitted_at": submission_row["submitted_at"].isoformat(),
            "last_updated_at": updated_at.isoformat(),
            "results": results,
            "summary": {
                "total": len(results),
                "correct": correct,
                "wrong": len(results) - correct - pending,
                "pending_review": pending,
                "accuracy": round(correct / len(results), 3) if results else 0.0,
            },
        }
        return self._public_submission(submission)

    async def request_hint(
        self,
        user: User,
        submission_id: str,
        payload: dict[str, Any],
        grade_problem: Callable[[JsonDict, str, int], Awaitable[JsonDict]],
    ) -> JsonDict:
        problem_id = self._uuid_text(payload["problem_id"], "problem_id")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            submission = await self._visible_submission(connection, user, submission_id)
            due_date = submission["due_date"]
            if due_date is not None and self._assignment_status(due_date) == "expired":
                raise AppError(410, 4006, "作业已截止，无法继续尝试")
            row = await connection.fetchrow(
                """
                SELECT p.id, p.problem_text, p.problem_type, p.grade_level, p.difficulty,
                       p.reference_answer, p.solution_steps, p.common_errors, p.tags, ap.position,
                       latest.answer_text AS previous_answer, latest.hint_level, latest.attempt_number,
                       latest.is_correct
                FROM assignment_problems ap
                JOIN problems p ON p.id = ap.problem_id AND NOT p.is_deleted
                LEFT JOIN LATERAL (
                    SELECT sa.answer_text, sa.hint_level, sa.attempt_number, gr.is_correct
                    FROM submission_answers sa
                    JOIN grading_results gr
                      ON gr.tenant_id = sa.tenant_id
                     AND gr.submission_id = sa.submission_id
                     AND gr.problem_id = sa.problem_id
                     AND gr.attempt_number = sa.attempt_number
                    WHERE sa.tenant_id = $1
                      AND sa.submission_id = $2
                      AND sa.problem_id = ap.problem_id
                    ORDER BY sa.attempt_number DESC
                    LIMIT 1
                ) latest ON true
                WHERE ap.tenant_id = $1 AND ap.assignment_id = $3 AND ap.problem_id = $4
                """,
                user.tenant_id,
                submission_id,
                submission["assignment_id"],
                problem_id,
            )
            if row is None or row["attempt_number"] is None:
                raise AppError(403, 4003, "这道题不属于你的提交记录")
            if row["is_correct"] is True:
                raise AppError(409, 4007, "该题已经答对，无需继续提交")
            previous_hint_level = int(row["hint_level"])
            previous_attempt_number = int(row["attempt_number"])
            if previous_hint_level >= 3:
                raise AppError(409, 4007, "该题已展示完整解法，无法继续提交")

            await connection.execute(
                """
                UPDATE human_review_queue hrq
                SET status = 'reviewed',
                    reviewed_at = now(),
                    reviewer_notes = COALESCE(hrq.reviewer_notes, 'superseded_by_student_hint')
                FROM grading_results gr
                WHERE hrq.tenant_id = gr.tenant_id
                  AND hrq.grading_result_id = gr.id
                  AND hrq.tenant_id = $1
                  AND gr.submission_id = $2
                  AND gr.problem_id = $3
                  AND hrq.status = 'pending'
                """,
                user.tenant_id,
                submission_id,
                problem_id,
            )

            problem = {
                "problem_id": problem_id,
                "problem_text": row["problem_text"],
                "problem_type": row["problem_type"],
                "grade_level": row["grade_level"],
                "difficulty": row["difficulty"],
                "reference_answer": row["reference_answer"],
                "solution_steps": self._json_value(row["solution_steps"], []),
                "common_errors": self._json_value(row["common_errors"], []),
                "tags": list(row["tags"] or []),
            }
            hint_level = previous_hint_level + 1
            attempt_number = previous_attempt_number + 1
            result = {
                "problem_id": problem_id,
                "sequence": int(row["position"]),
                "problem_text": row["problem_text"],
                **await grade_problem(problem, str(payload["new_answer"]).strip(), hint_level),
            }
            result["hint_level"] = hint_level
            result["attempt_number"] = attempt_number

            await connection.execute(
                """
                INSERT INTO submission_answers (
                    tenant_id, submission_id, problem_id, answer_text, hint_level, attempt_number
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                user.tenant_id,
                submission_id,
                problem_id,
                result["student_answer"],
                hint_level,
                attempt_number,
            )
            grading_id = await connection.fetchval(
                """
                INSERT INTO grading_results (
                    tenant_id, submission_id, problem_id, attempt_number, is_correct,
                    confidence_score, error_type, feedback_text, encouragement, next_hint,
                    routed_to_human, human_review_reason, source, agent_trace
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11, $12, $13, $14::jsonb
                )
                RETURNING id
                """,
                user.tenant_id,
                submission_id,
                problem_id,
                attempt_number,
                result["is_correct"],
                result["confidence_score"],
                result["error_type"],
                result["feedback_text"],
                result["encouragement"],
                result["next_hint"],
                result["routed_to_human"],
                "low_confidence" if result["routed_to_human"] else None,
                result["grading_source"],
                json.dumps(result.get("agent_trace", []), ensure_ascii=False),
            )
            if result["routed_to_human"]:
                await connection.execute(
                    """
                    INSERT INTO human_review_queue (tenant_id, grading_result_id, reason)
                    VALUES ($1, $2, 'low_confidence')
                    """,
                    user.tenant_id,
                    grading_id,
                )
            await self._set_submission_status(connection, user.tenant_id, submission_id)

        public = {key: value for key, value in result.items() if key != "agent_trace"}
        public["remaining_hints"] = 3 - hint_level
        return public

    @staticmethod
    def _submission_visible_to(user: User, class_ids: set[str], student_id: str) -> bool:
        if user.role in {"admin", "sysadmin"}:
            return True
        if user.role == "student":
            return student_id == user.user_id
        return bool(set(user.class_ids) & class_ids)

    @staticmethod
    def _summary(results: list[JsonDict]) -> JsonDict:
        correct = sum(result["is_correct"] is True for result in results)
        pending = sum(bool(result["routed_to_human"]) for result in results)
        return {
            "total": len(results),
            "correct": correct,
            "wrong": len(results) - correct - pending,
            "pending_review": pending,
            "accuracy": round(correct / len(results), 3) if results else 0.0,
        }

    async def _set_submission_status(
        self,
        connection: asyncpg.Connection,
        tenant_id: str,
        submission_id: str,
        *,
        reviewed: bool = False,
    ) -> tuple[str, datetime]:
        pending = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM (
                SELECT DISTINCT ON (problem_id) problem_id, routed_to_human
                FROM grading_results
                WHERE tenant_id = $1 AND submission_id = $2
                ORDER BY problem_id, attempt_number DESC
            ) latest
            WHERE routed_to_human
            """,
            tenant_id,
            submission_id,
        )
        status = "partial_human_review" if int(pending or 0) else ("reviewed" if reviewed else "graded")
        updated_at = await connection.fetchval(
            """
            UPDATE submissions SET status = $3
            WHERE tenant_id = $1 AND id = $2
            RETURNING updated_at
            """,
            tenant_id,
            submission_id,
            status,
        )
        return status, updated_at

    async def _visible_submission(
        self,
        connection: asyncpg.Connection,
        user: User,
        submission_id: str,
    ) -> Mapping[str, Any]:
        submission = await connection.fetchrow(
            """
            SELECT s.id, s.assignment_id, s.student_id, s.status, s.submitted_at, s.updated_at,
                   a.title AS assignment_title, a.due_date,
                   array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids
            FROM submissions s
            JOIN assignments a ON a.tenant_id = s.tenant_id AND a.id = s.assignment_id AND NOT a.is_deleted
            JOIN assignment_classes ac ON ac.tenant_id = s.tenant_id AND ac.assignment_id = s.assignment_id
            WHERE s.tenant_id = $1 AND s.id = $2
            GROUP BY s.id, s.assignment_id, s.student_id, s.status, s.submitted_at, s.updated_at, a.title, a.due_date
            """,
            user.tenant_id,
            submission_id,
        )
        if submission is None:
            raise AppError(404, 4004, "提交记录不存在")
        class_ids = {str(value) for value in submission["class_ids"]}
        if not self._submission_visible_to(user, class_ids, str(submission["student_id"])):
            raise AppError(404, 4004, "提交记录不存在")
        return submission

    async def list_submissions(
        self,
        user: User,
        *,
        student_id: str | None,
        assignment_id: str | None,
        page_number: int,
        page_size: int,
    ) -> JsonDict:
        if user.role == "student":
            student_id = user.user_id
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            rows = await connection.fetch(
                """
                SELECT s.id, s.assignment_id, s.student_id, s.status, s.submitted_at, s.updated_at,
                       array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids
                FROM submissions s
                JOIN assignment_classes ac ON ac.tenant_id = s.tenant_id AND ac.assignment_id = s.assignment_id
                WHERE s.tenant_id = $1
                  AND ($2::uuid IS NULL OR s.student_id = $2::uuid)
                  AND ($3::uuid IS NULL OR s.assignment_id = $3::uuid)
                GROUP BY s.id, s.assignment_id, s.student_id, s.status, s.submitted_at, s.updated_at
                ORDER BY s.submitted_at DESC, s.id DESC
                """,
                user.tenant_id,
                student_id,
                assignment_id,
            )
        items: list[JsonDict] = []
        for row in rows:
            class_ids = {str(value) for value in row["class_ids"]}
            if not self._submission_visible_to(user, class_ids, str(row["student_id"])):
                continue
            items.append(
                {
                    "submission_id": str(row["id"]),
                    "assignment_id": str(row["assignment_id"]),
                    "student_id": str(row["student_id"]),
                    "status": row["status"],
                    "submitted_at": row["submitted_at"].isoformat(),
                    "last_updated_at": row["updated_at"].isoformat(),
                }
            )
        return self._page(items, page_number, page_size)

    async def submission_detail(self, user: User, submission_id: str) -> JsonDict:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            submission = await self._visible_submission(connection, user, submission_id)
            rows = await connection.fetch(
                """
                SELECT *
                FROM (
                    SELECT DISTINCT ON (gr.problem_id)
                           gr.problem_id, gr.attempt_number, gr.is_correct, gr.confidence_score,
                           gr.error_type, gr.feedback_text, gr.encouragement, gr.next_hint,
                           gr.routed_to_human, gr.source, gr.agent_trace,
                           sa.answer_text AS student_answer, sa.hint_level,
                           p.problem_text, ap.position
                    FROM grading_results gr
                    JOIN submission_answers sa
                      ON sa.tenant_id = gr.tenant_id
                     AND sa.submission_id = gr.submission_id
                     AND sa.problem_id = gr.problem_id
                     AND sa.attempt_number = gr.attempt_number
                    JOIN problems p ON p.id = gr.problem_id
                    JOIN assignment_problems ap
                      ON ap.tenant_id = gr.tenant_id
                     AND ap.assignment_id = $3
                     AND ap.problem_id = gr.problem_id
                    WHERE gr.tenant_id = $1 AND gr.submission_id = $2
                    ORDER BY gr.problem_id, gr.attempt_number DESC
                ) latest
                ORDER BY position
                """,
                user.tenant_id,
                submission_id,
                submission["assignment_id"],
            )
        results: list[JsonDict] = []
        for row in rows:
            results.append(
                {
                    "problem_id": str(row["problem_id"]),
                    "sequence": int(row["position"]),
                    "problem_text": row["problem_text"],
                    "student_answer": row["student_answer"],
                    "is_correct": row["is_correct"],
                    "confidence_score": row["confidence_score"],
                    "feedback_text": row["feedback_text"],
                    "encouragement": row["encouragement"],
                    "next_hint": row["next_hint"],
                    "error_type": row["error_type"],
                    "hint_level": row["hint_level"],
                    "attempt_number": row["attempt_number"],
                    "routed_to_human": row["routed_to_human"],
                    "grading_source": row["source"],
                    "agent_trace": self._json_value(row["agent_trace"], []),
                }
            )
        submission_data = {
            "submission_id": str(submission["id"]),
            "status": submission["status"],
            "submitted_at": submission["submitted_at"].isoformat(),
            "last_updated_at": submission["updated_at"].isoformat(),
            "results": results,
            "summary": self._summary(results),
        }
        return self._public_submission(submission_data)

    async def submission_event_snapshot(self, ticket: Ticket) -> JsonDict:
        async with self._connection(ticket.tenant_id, ticket.role, ticket.user_id) as connection:
            submission = await connection.fetchrow(
                """
                SELECT id, status, updated_at
                FROM submissions
                WHERE tenant_id = $1 AND id = $2
                """,
                ticket.tenant_id,
                ticket.submission_id,
            )
            if submission is None:
                raise AppError(404, 4004, "提交记录不存在")
            rows = await connection.fetch(
                """
                SELECT *
                FROM (
                    SELECT DISTINCT ON (problem_id)
                           problem_id, is_correct, routed_to_human
                    FROM grading_results
                    WHERE tenant_id = $1 AND submission_id = $2
                    ORDER BY problem_id, attempt_number DESC
                ) latest
                ORDER BY problem_id
                """,
                ticket.tenant_id,
                ticket.submission_id,
            )
        results = [
            {
                "problem_id": str(row["problem_id"]),
                "is_correct": row["is_correct"],
                "routed_to_human": row["routed_to_human"],
            }
            for row in rows
        ]
        return {
            "submission_id": str(submission["id"]),
            "status": submission["status"],
            "last_updated_at": submission["updated_at"].isoformat(),
            "summary": self._summary(results),
        }

    def _public_review(self, row: Mapping[str, Any]) -> JsonDict:
        return {
            "review_id": str(row["review_id"]),
            "tenant_id": str(row["tenant_id"]),
            "submission_id": str(row["submission_id"]),
            "problem_id": str(row["problem_id"]),
            "student_name": row["student_name"],
            "class_name": "、".join(row["class_names"] or []),
            "assignment_title": row["assignment_title"],
            "problem_text": row["problem_text"],
            "problem_type": row["problem_type"],
            "student_answer": row["student_answer"],
            "reference_answer": row["reference_answer"],
            "ai_conclusion": "待审核" if row["is_correct"] is None else ("正确" if row["is_correct"] else "错误"),
            "ai_confidence": row["confidence_score"],
            "human_review_reason": row["reason"],
            "status": row["status"],
            "created_at": row["created_at"].isoformat(),
            "agent_trace": self._json_value(row["agent_trace"], []),
            "reviewer_notes": row["reviewer_notes"],
            "is_training_example": row["is_training_example"],
        }

    async def _review_rows(self, connection: asyncpg.Connection, user: User, *, review_id: str | None = None) -> list:
        rows = await connection.fetch(
            """
            SELECT hrq.id AS review_id, hrq.tenant_id, hrq.reason, hrq.status, hrq.created_at,
                   hrq.reviewer_notes, hrq.is_training_example,
                   gr.id AS grading_result_id, gr.submission_id, gr.problem_id, gr.is_correct,
                   gr.confidence_score, gr.agent_trace,
                   sa.answer_text AS student_answer,
                   s.student_id,
                   u.display_name AS student_name,
                   a.title AS assignment_title,
                   p.problem_text, p.problem_type, p.reference_answer,
                   array_agg(DISTINCT c.name ORDER BY c.name) AS class_names,
                   array_agg(DISTINCT ac.class_id ORDER BY ac.class_id) AS class_ids
            FROM human_review_queue hrq
            JOIN grading_results gr ON gr.tenant_id = hrq.tenant_id AND gr.id = hrq.grading_result_id
            JOIN submission_answers sa
              ON sa.tenant_id = gr.tenant_id
             AND sa.submission_id = gr.submission_id
             AND sa.problem_id = gr.problem_id
             AND sa.attempt_number = gr.attempt_number
            JOIN submissions s ON s.tenant_id = gr.tenant_id AND s.id = gr.submission_id
            JOIN users u ON u.tenant_id = s.tenant_id AND u.id = s.student_id
            JOIN assignments a ON a.tenant_id = s.tenant_id AND a.id = s.assignment_id
            JOIN assignment_classes ac ON ac.tenant_id = s.tenant_id AND ac.assignment_id = s.assignment_id
            JOIN classes c ON c.tenant_id = ac.tenant_id AND c.id = ac.class_id
            JOIN problems p ON p.id = gr.problem_id
            WHERE hrq.tenant_id = $1 AND ($2::uuid IS NULL OR hrq.id = $2::uuid)
            GROUP BY hrq.id, hrq.tenant_id, hrq.reason, hrq.status, hrq.created_at,
                     hrq.reviewer_notes, hrq.is_training_example, gr.id, gr.submission_id,
                     gr.problem_id, gr.is_correct, gr.confidence_score, gr.agent_trace,
                     sa.answer_text, s.student_id, u.display_name, a.title,
                     p.problem_text, p.problem_type, p.reference_answer
            ORDER BY hrq.created_at DESC, hrq.id DESC
            """,
            user.tenant_id,
            review_id,
        )
        visible = []
        for row in rows:
            class_ids = {str(value) for value in row["class_ids"]}
            if self._submission_visible_to(user, class_ids, str(row["student_id"])):
                visible.append(row)
        return visible

    async def list_human_reviews(
        self,
        user: User,
        *,
        status: str,
        page_number: int,
        page_size: int,
    ) -> tuple[JsonDict, int]:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            rows = await self._review_rows(connection, user)
        pending_count = sum(row["status"] == "pending" for row in rows)
        filtered = [row for row in rows if status == "all" or row["status"] == status]
        return self._page([self._public_review(row) for row in filtered], page_number, page_size), pending_count

    async def human_review_detail(self, user: User, review_id: str) -> JsonDict:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            rows = await self._review_rows(connection, user, review_id=self._uuid_text(review_id, "review_id"))
        if not rows:
            raise AppError(404, 4004, "审核记录不存在")
        return self._public_review(rows[0])

    async def resolve_human_review(self, user: User, review_id: str, payload: dict[str, Any]) -> JsonDict:
        normalized_review_id = self._uuid_text(review_id, "review_id")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            rows = await self._review_rows(connection, user, review_id=normalized_review_id)
            if not rows or rows[0]["status"] != "pending":
                raise AppError(404, 4004, "待审核记录不存在")
            row = rows[0]
            feedback = payload.get("override_feedback") or f"{row['student_answer']}（已经过老师审核）"
            await connection.execute(
                """
                UPDATE grading_results
                SET is_correct = $4,
                    error_type = $5,
                    feedback_text = $6,
                    routed_to_human = false,
                    human_review_reason = NULL,
                    source = 'human_override',
                    confidence_score = 1.0
                WHERE tenant_id = $1 AND id = $2 AND submission_id = $3
                """,
                user.tenant_id,
                row["grading_result_id"],
                row["submission_id"],
                payload["override_correct"],
                None if payload["override_correct"] else payload["override_error_type"],
                feedback,
            )
            await connection.execute(
                """
                UPDATE human_review_queue
                SET status = 'reviewed',
                    reviewer_id = $3,
                    reviewed_at = now(),
                    override_correct = $4,
                    override_error_type = $5,
                    override_feedback = $6,
                    reviewer_notes = $7,
                    is_training_example = $8
                WHERE tenant_id = $1 AND id = $2
                """,
                user.tenant_id,
                normalized_review_id,
                user.user_id,
                payload["override_correct"],
                None if payload["override_correct"] else payload["override_error_type"],
                payload.get("override_feedback"),
                payload.get("reviewer_notes"),
                payload["is_training_example"],
            )
            await self._set_submission_status(connection, user.tenant_id, str(row["submission_id"]), reviewed=True)
        return {
            "review_id": normalized_review_id,
            "status": "reviewed",
            "override_correct": payload["override_correct"],
            "student_notified": True,
            "notify_eta_seconds": 0,
            "is_training_example": payload["is_training_example"],
        }

    async def create_class(self, user: User, payload: dict[str, Any]) -> JsonDict:
        teacher_id = self._uuid_text(payload["teacher_id"], "teacher_id")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            teacher = await connection.fetchrow(
                """
                SELECT id FROM users
                WHERE tenant_id = $1 AND id = $2 AND role = 'teacher' AND NOT is_deleted
                """,
                user.tenant_id,
                teacher_id,
            )
            if teacher is None:
                raise AppError(404, 4004, "教师不存在")
            row = await connection.fetchrow(
                """
                INSERT INTO classes (tenant_id, grade_level, name, teacher_id, academic_year)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, created_at
                """,
                user.tenant_id,
                payload["grade_level"],
                payload["name"],
                teacher_id,
                payload["academic_year"],
            )
        return {
            "class_id": str(row["id"]),
            "name": payload["name"],
            "grade_level": payload["grade_level"],
            "teacher_id": teacher_id,
            "academic_year": payload["academic_year"],
            "created_at": row["created_at"].isoformat(),
        }

    async def admin_stats_overview(self, user: User) -> JsonDict:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            tenant = await connection.fetchrow(
                "SELECT name, active_school_year FROM tenants WHERE id = $1",
                user.tenant_id,
            )
            user_counts = await connection.fetchrow(
                """
                SELECT
                  COUNT(*) FILTER (WHERE role = 'student') AS students,
                  COUNT(*) FILTER (WHERE role = 'teacher') AS teachers,
                  COUNT(*) FILTER (
                    WHERE role = 'student' AND last_login_at >= now() - interval '1 day'
                  ) AS active_students,
                  COUNT(*) FILTER (
                    WHERE role = 'teacher' AND last_login_at >= now() - interval '1 day'
                  ) AS active_teachers
                FROM users
                WHERE tenant_id = $1 AND NOT is_deleted
                """,
                user.tenant_id,
            )
            class_count = await connection.fetchval(
                "SELECT COUNT(*) FROM classes WHERE tenant_id = $1 AND NOT is_deleted",
                user.tenant_id,
            )
            submission_counts = await connection.fetchrow(
                """
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE submitted_at >= date_trunc('day', now())) AS today,
                  COUNT(*) FILTER (WHERE submitted_at >= date_trunc('week', now())) AS week,
                  COUNT(*) FILTER (WHERE submitted_at >= date_trunc('month', now())) AS month
                FROM submissions
                WHERE tenant_id = $1
                """,
                user.tenant_id,
            )
            grading = await connection.fetchrow(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (submission_id, problem_id)
                         is_correct, source, routed_to_human
                  FROM grading_results
                  WHERE tenant_id = $1
                  ORDER BY submission_id, problem_id, attempt_number DESC
                )
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE source <> 'human_override') AS ai_graded,
                  COUNT(*) FILTER (WHERE source = 'human_override') AS human_review,
                  COUNT(*) FILTER (WHERE source = 'rule_fallback') AS rule_fallback,
                  COUNT(*) FILTER (WHERE is_correct IS TRUE) AS correct
                FROM latest
                """,
                user.tenant_id,
            )
        total_results = int(grading["total"] or 0)
        human_review_count = int(grading["human_review"] or 0)
        rule_fallback_count = int(grading["rule_fallback"] or 0)
        correct = int(grading["correct"] or 0)
        return {
            "tenant_name": tenant["name"] if tenant else user.tenant_id,
            "active_school_year": tenant["active_school_year"] if tenant else None,
            "users": {
                "total_students": int(user_counts["students"] or 0),
                "total_teachers": int(user_counts["teachers"] or 0),
                "total_classes": int(class_count or 0),
                "active_students_today": int(user_counts["active_students"] or 0),
                "active_teachers_today": int(user_counts["active_teachers"] or 0),
            },
            "submissions": {
                "total_all_time": int(submission_counts["total"] or 0),
                "today": int(submission_counts["today"] or 0),
                "this_week": int(submission_counts["week"] or 0),
                "this_month": int(submission_counts["month"] or 0),
            },
            "grading": {
                "ai_graded_count": int(grading["ai_graded"] or 0),
                "human_review_count": human_review_count,
                "human_review_rate": round(human_review_count / total_results, 3) if total_results else 0.0,
                "average_accuracy": round(correct / total_results, 3) if total_results else 0.0,
                "rule_fallback_rate": round(rule_fallback_count / total_results, 3) if total_results else 0.0,
            },
            "performance": {"avg_grading_latency_ms": 0, "p95_grading_latency_ms": 0},
        }

    async def reset_user_password(self, user: User, target_user_id: str, password_hash: bytes) -> JsonDict:
        normalized_user_id = self._uuid_text(target_user_id, "user_id")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            row = await connection.fetchrow(
                """
                UPDATE users
                SET password_hash = $3,
                    force_change_password = true,
                    token_version = token_version + 1
                WHERE tenant_id = $1 AND id = $2 AND NOT is_deleted
                RETURNING id, username, display_name
                """,
                user.tenant_id,
                normalized_user_id,
                password_hash.decode("utf-8"),
            )
            if row is None:
                raise AppError(404, 4004, "用户不存在")
            await connection.execute(
                """
                INSERT INTO audit_logs (tenant_id, operator_id, action, resource_type, resource_id, result)
                VALUES ($1, $2, 'reset_password', 'user', $3, 'success')
                """,
                user.tenant_id,
                user.user_id,
                normalized_user_id,
            )
        return {
            "user_id": str(row["id"]),
            "username": row["username"],
            "display_name": row["display_name"],
            "force_change_on_next_login": True,
        }

    async def run_harness(self, user: User, payload: dict[str, Any], report: dict[str, Any]) -> JsonDict:
        metrics = report["metrics"]
        failures = list(report["failures"])
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            run_id = await connection.fetchval(
                """
                INSERT INTO harness_runs (
                    status, triggered_by, prompt_version, use_mock, total_cases, passed_cases,
                    failed_cases_json, accuracy, false_positive_rate, false_negative_rate,
                    error_cls_accuracy, calibration_error, coverage_matrix, passed,
                    accuracy_threshold, duration_seconds
                )
                VALUES (
                    'completed', 'manual', 'local', $1, $2, $3,
                    $4::jsonb, $5, $6, $7, NULL, NULL, '{}'::jsonb, $8, 0.94, 0
                )
                RETURNING id
                """,
                payload["use_mock"],
                metrics["total"],
                metrics["total"] - len(failures),
                json.dumps(failures, ensure_ascii=False),
                metrics["accuracy"],
                metrics["false_positive_rate"],
                metrics["false_negative_rate"],
                not failures,
            )
        return {
            "run_id": str(run_id),
            "status": "completed",
            "estimated_seconds": 0,
            "use_mock": payload["use_mock"],
            "total_cases": metrics["total"],
        }

    async def harness_run_detail(self, user: User, run_id: str) -> JsonDict:
        normalized_run_id = self._uuid_text(run_id, "run_id")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            row = await connection.fetchrow(
                """
                SELECT id, status, passed, prompt_version, use_mock, total_cases, passed_cases,
                       failed_cases_json, accuracy, false_positive_rate, false_negative_rate,
                       error_cls_accuracy, calibration_error, coverage_matrix, run_at, duration_seconds
                FROM harness_runs
                WHERE id = $1
                """,
                normalized_run_id,
            )
        if row is None:
            raise AppError(404, 4004, "Harness 运行记录不存在")
        return {
            "run_id": str(row["id"]),
            "status": row["status"],
            "passed": row["passed"],
            "prompt_version": row["prompt_version"],
            "use_mock": row["use_mock"],
            "total_cases": row["total_cases"],
            "passed_cases": row["passed_cases"],
            "accuracy": row["accuracy"],
            "false_positive_rate": row["false_positive_rate"],
            "false_negative_rate": row["false_negative_rate"],
            "error_cls_accuracy": row["error_cls_accuracy"],
            "calibration_error": row["calibration_error"],
            "coverage_matrix": self._json_value(row["coverage_matrix"], {}),
            "failed_cases": self._json_value(row["failed_cases_json"], []),
            "run_at": row["run_at"].isoformat(),
            "duration_seconds": row["duration_seconds"],
        }

    async def create_rag_ingest_job(self, user: User, payload: dict[str, Any]) -> JsonDict:
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            matched = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM problems
                WHERE tenant_id = $1
                  AND NOT is_deleted
                  AND (cardinality($2::int[]) = 0 OR grade_level = ANY($2::int[]))
                """,
                user.tenant_id,
                payload["grade_levels"],
            )
            result = {
                "source": payload["source"],
                "matched_problem_count": int(matched or 0),
                "ingested_count": 0,
                "qdrant_status": "not_wired",
            }
            job = await connection.fetchrow(
                """
                INSERT INTO jobs (tenant_id, job_type, status, payload, result, attempts, created_by)
                VALUES ($1, 'rag_ingest', 'succeeded', $2::jsonb, $3::jsonb, 1, $4)
                RETURNING id, status
                """,
                user.tenant_id,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                user.user_id,
            )
        return {
            "job_id": str(job["id"]),
            "status": job["status"],
            "matched_problem_count": result["matched_problem_count"],
        }

    async def job_detail(self, user: User, job_id: str) -> JsonDict:
        normalized_job_id = self._uuid_text(job_id, "job_id")
        async with self._connection(user.tenant_id, user.role, user.user_id) as connection:
            row = await connection.fetchrow(
                """
                SELECT id, job_type, status, payload, result, error_message, created_by, created_at, updated_at
                FROM jobs
                WHERE tenant_id = $1 AND id = $2
                """,
                user.tenant_id,
                normalized_job_id,
            )
        if row is None or (user.role == "admin" and str(row["created_by"]) != user.user_id):
            raise AppError(404, 4004, "后台任务不存在")
        status = row["status"]
        progress = 1.0 if status in {"succeeded", "failed", "cancelled"} else 0.5
        return {
            "job_id": str(row["id"]),
            "job_type": row["job_type"],
            "status": status,
            "progress": progress,
            "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(),
            "payload": self._json_value(row["payload"], {}),
            "result": self._json_value(row["result"], None),
            "error_message": row["error_message"],
        }
