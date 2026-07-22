from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterable, Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse

from app.core.errors import AppError, envelope, utcnow
from app.core.security import create_access_token, hash_password, verify_password
from app.domain.models import Ticket, User
from app.domain.repository import IdentityProblemRepository, Repository
from app.exports import (
    XLSX_MEDIA_TYPE,
    assignment_export_filename,
    build_assignment_report_xlsx,
    build_problem_import_template_xlsx,
)
from app.grading import DeepSeekGradingClient, GradeRequest, LLMUnavailableError, LLMVerdict, QuestionType, route_grade
from app.harness import HarnessRunner
from app.imports import parse_problem_import, parse_student_import

from .dependencies import current_user, get_identity_repository, get_llm_grader, get_store, require_roles
from .schemas import (
    AdminResetPasswordRequest,
    AdminUserStatusRequest,
    AssignmentCreate,
    AssignmentPatch,
    ChangePasswordRequest,
    ClassCreate,
    HarnessRunRequest,
    HintRequest,
    LoginRequest,
    ProblemCreate,
    RagIngestRequest,
    ReviewRequest,
    SubmissionCreate,
    TicketRequest,
)

router = APIRouter()
HARNESS_DATASET = Path(__file__).resolve().parents[2] / "harness" / "dataset" / "grading_cases.jsonl"


def ident() -> str:
    return str(uuid4())


def iso_now() -> str:
    return utcnow().isoformat()


def sse_event_stream(
    events: Iterable[dict],
    *,
    heartbeat_count: int | None = 1,
    follow: bool = False,
    poll_seconds: float = 15.0,
) -> Iterator[str]:
    """Stream the mutable event list and keep followed connections alive."""
    event_list = events if isinstance(events, list) else list(events)
    cursor = 0
    heartbeats = 0
    while True:
        while cursor < len(event_list):
            event = event_list[cursor]
            cursor += 1
            yield f"id: {cursor}\nevent: grading_update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        if not follow and (heartbeat_count is None or heartbeats >= heartbeat_count):
            return
        if follow:
            time.sleep(poll_seconds)
        yield ": heartbeat\n\n"
        heartbeats += 1


async def postgres_sse_event_stream(
    repository: IdentityProblemRepository,
    ticket: Ticket,
    *,
    follow: bool,
    poll_seconds: float = 15.0,
) -> AsyncIterator[str]:
    cursor = 0
    last_updated_at: str | None = None
    while True:
        snapshot = await repository.submission_event_snapshot(ticket)
        if snapshot["last_updated_at"] != last_updated_at:
            last_updated_at = snapshot["last_updated_at"]
            cursor += 1
            yield f"id: {cursor}\nevent: grading_update\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
            if not follow:
                return
        if not follow:
            yield ": heartbeat\n\n"
            return
        await asyncio.sleep(poll_seconds)
        yield ": heartbeat\n\n"


def page(items: list[dict], page_number: int, page_size: int) -> dict:
    start = (page_number - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": len(items),
        "page": page_number,
        "page_size": page_size,
        "has_next": start + page_size < len(items),
    }


def can_access_assignment(user: User, assignment: dict) -> bool:
    return user.tenant_id == assignment["tenant_id"] and (
        user.role in {"admin", "sysadmin"} or bool(set(user.class_ids) & set(assignment["class_ids"]))
    )


def as_utc(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def assignment_status(assignment: dict) -> str:
    due_date = assignment.get("due_date")
    return "expired" if due_date and as_utc(due_date) <= utcnow() else "active"


def assignment_class_name(store: Repository, assignment: dict) -> str:
    return "、".join(store.class_name(assignment["tenant_id"], class_id) for class_id in assignment["class_ids"])


def can_access_submission(user: User, submission: dict) -> bool:
    if user.tenant_id != submission["tenant_id"]:
        return False
    if user.role in {"admin", "sysadmin"}:
        return True
    if user.role == "student":
        return submission["student_id"] == user.user_id
    return user.role == "teacher" and bool(set(user.class_ids) & set(submission["class_ids"]))


def validate_class_ids(store: Repository, user: User, class_ids: list[str]) -> None:
    if len(class_ids) != len(set(class_ids)):
        raise AppError(422, 4022, "请求参数校验失败", "class_ids must be unique")
    if not set(class_ids).issubset(store.known_class_ids(user.tenant_id)):
        raise AppError(404, 4004, "班级不存在")
    if user.role == "teacher" and not set(class_ids).issubset(user.class_ids):
        raise AppError(403, 4003, "教师只能向本人班级布置作业")


def validate_future_due_date(due_date) -> None:
    if due_date is None:
        return
    due = due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)
    if due <= utcnow():
        raise AppError(422, 4022, "请求参数校验失败", "due_date must be in the future")


def get_assignment(store: Repository, assignment_id: str) -> dict:
    assignment = store.assignments.get(assignment_id)
    if assignment is None:
        raise AppError(404, 4004, "作业不存在")
    return assignment


def get_visible_submission(store: Repository, submission_id: str, user: User) -> dict:
    submission = store.submissions.get(submission_id)
    if submission is None or submission["tenant_id"] != user.tenant_id:
        raise AppError(404, 4004, "提交记录不存在")
    if not can_access_submission(user, submission):
        raise AppError(404, 4004, "提交记录不存在")
    return submission


def public_submission(submission: dict) -> dict:
    keys = ("submission_id", "status", "submitted_at", "results", "summary", "last_updated_at")
    public = {key: submission[key] for key in keys if key in submission}
    if "results" in public:
        public["results"] = [
            {key: value for key, value in result.items() if key != "agent_trace"} for result in public["results"]
        ]
    return public


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    configured = request.app.state.settings
    user = await repository.identity_by_username(payload.username)
    now = utcnow()
    if user is not None and user.locked_until and user.locked_until > now:
        remaining = max(1, int((user.locked_until - now).total_seconds() / 60) + 1)
        raise AppError(
            401,
            4001,
            f"账户已锁定，请 {remaining} 分钟后重试",
            locked_until=user.locked_until.isoformat(),
        )
    valid = user is not None and verify_password(payload.password, user.password_hash)
    if not valid:
        if user is not None:
            user = await repository.register_login_failure(
                user,
                max_failures=configured.login_max_failures,
                locked_until=now + timedelta(minutes=configured.login_lock_minutes),
            )
            if user.failed_logins >= configured.login_max_failures:
                effective_lock = user.locked_until or now + timedelta(minutes=configured.login_lock_minutes)
                raise AppError(
                    401,
                    4001,
                    "账户已锁定，请 15 分钟后重试",
                    locked_until=effective_lock.isoformat(),
                )
        raise AppError(401, 4001, "用户名或密码错误")
    user = cast(User, user)  # narrowed after the invalid-credential branch
    await repository.clear_login_failures(user)
    token = create_access_token(user, configured)
    data = {
        "access_token": token,
        "token_type": "bearer",  # nosec B105
        "expires_in": configured.jwt_expires_seconds,
        "user": {
            "user_id": user.user_id,
            "display_name": user.display_name,
            "username": user.username,
            "role": user.role,
            "grade_level": user.grade_level,
            "tenant_id": user.tenant_id,
            "force_change_password": user.force_change_password,
        },
    }
    return envelope(request, data)


@router.post("/auth/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(current_user),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise AppError(401, 4001, "旧密码错误")
    if user.role in {"admin", "sysadmin"} and (
        len(payload.new_password) < 8
        or not any(c.isalpha() for c in payload.new_password)
        or not any(c.isdigit() for c in payload.new_password)
    ):
        raise AppError(422, 4022, "请求参数校验失败", "Admin password requires letters and digits")
    await repository.replace_password(user, hash_password(payload.new_password, request.app.state.settings))
    return envelope(request)


@router.post("/auth/logout")
async def logout(
    request: Request,
    user: User = Depends(current_user),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    await repository.increment_token_version(user)
    return envelope(request, message="已退出登录")


@router.post("/auth/sse-ticket")
async def sse_ticket(
    payload: TicketRequest,
    request: Request,
    user: User = Depends(current_user),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        await repository.submission_detail(user, payload.submission_id)
    else:
        get_visible_submission(store, payload.submission_id, user)
    configured = request.app.state.settings
    value = await request.app.state.ticket_repository.issue(
        Ticket(
            user.user_id,
            user.tenant_id,
            payload.submission_id,
            user.role,
            utcnow() + timedelta(seconds=configured.sse_ticket_ttl_seconds),
        ),
        configured.sse_ticket_ttl_seconds,
    )
    return envelope(request, {"ticket": value, "expires_in": configured.sse_ticket_ttl_seconds})


@router.post("/problems/", status_code=201)
async def create_problem(
    payload: ProblemCreate,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    problem_id = await repository.create_catalog_problem(user, payload.model_dump())
    return envelope(request, {"problem_id": problem_id, "embedding_status": "pending", "message": "题目已创建"})


@router.post("/problems/bulk-import", status_code=202)
async def bulk_import_problems(
    request: Request,
    file: UploadFile = File(...),
    curriculum_version: str = Form("renjiao"),
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    try:
        imported_rows = parse_problem_import(file.filename or "problems.csv", await file.read())
    except (UnicodeDecodeError, ValueError) as exc:
        raise AppError(422, 4022, "请求参数校验失败", str(exc)) from exc
    rows = [
        {
            "row": row.row_number,
            "problem_text": row.problem_text,
            "problem_type": row.problem_type,
            "reference_answer": row.reference_answer,
            "grade_level": row.grade_level,
            "difficulty": row.difficulty,
            "solution_steps": row.solution_steps,
            "common_errors": row.common_errors,
            "tags": row.tags,
        }
        for row in imported_rows
    ]
    return envelope(
        request, await repository.bulk_import_problems(user, rows, {"curriculum_version": curriculum_version})
    )


@router.get("/problems/bulk-import/template")
async def problem_import_template(
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
):
    return Response(
        content=build_problem_import_template_xlsx(),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="problem_import_template.xlsx"'},
    )


@router.get("/problems/")
async def list_problems(
    request: Request,
    grade_level: int | None = Query(None, ge=1, le=6),
    problem_type: str | None = None,
    difficulty: str | None = None,
    keyword: str | None = None,
    page_number: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    data = await repository.list_catalog_problems(
        user,
        grade_level=grade_level,
        problem_type=problem_type,
        difficulty=difficulty,
        keyword=keyword,
        page_number=page_number,
        page_size=page_size,
    )
    return envelope(request, data)


@router.delete("/problems/{problem_id}")
async def delete_problem(
    problem_id: str,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.delete_catalog_problem(user, problem_id))


@router.post("/assignments/", status_code=201)
async def create_assignment(
    payload: AssignmentCreate,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    validate_future_due_date(payload.due_date)
    data = await repository.create_assignment(user, payload.model_dump())
    return envelope(request, data)


@router.get("/assignments/")
async def list_assignments(
    request: Request,
    class_id: str | None = None,
    status: Literal["active", "expired", "all"] = "all",
    order_by: Literal["created_at", "due_date"] | None = None,
    order: Literal["asc", "desc"] | None = None,
    page_number: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(current_user),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    effective_order_by = order_by or ("due_date" if user.role == "student" else "created_at")
    effective_order = order or ("asc" if user.role == "student" and order_by is None else "desc")
    data = await repository.list_assignments(
        user,
        class_id=class_id,
        status=status,
        order_by=effective_order_by,
        order=effective_order,
        page_number=page_number,
        page_size=page_size,
    )
    return envelope(request, data)


@router.get("/assignments/{assignment_id}")
async def assignment_detail(
    assignment_id: str,
    request: Request,
    user: User = Depends(current_user),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.assignment_detail(user, assignment_id))


@router.get("/assignments/{assignment_id}/stats")
async def assignment_stats(
    assignment_id: str,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.assignment_stats(user, assignment_id))


@router.patch("/assignments/{assignment_id}")
async def patch_assignment(
    assignment_id: str,
    payload: AssignmentPatch,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        validate_future_due_date(payload.due_date if "due_date" in payload.model_fields_set else None)
        data = await repository.patch_assignment(user, assignment_id, payload.model_dump(exclude_unset=True))
        return envelope(request, data)
    assignment = get_assignment(store, assignment_id)
    if not can_access_assignment(user, assignment):
        raise AppError(404, 4004, "作业不存在")
    if user.role == "teacher" and not set(assignment["class_ids"]).issubset(user.class_ids):
        raise AppError(403, 4003, "教师只能修改完全属于本人班级的作业")
    if assignment_status(assignment) == "expired":
        raise AppError(409, 4005, "作业已截止，不可修改")
    if (
        len(payload.add_problem_ids) != len(set(payload.add_problem_ids))
        or len(payload.remove_problem_ids) != len(set(payload.remove_problem_ids))
        or set(payload.add_problem_ids) & set(payload.remove_problem_ids)
    ):
        raise AppError(422, 4022, "请求参数校验失败", "problem patch ids must be unique and disjoint")
    all_ids = payload.add_problem_ids + payload.remove_problem_ids
    if any(pid not in store.problems or store.problems[pid]["tenant_id"] != user.tenant_id for pid in all_ids):
        raise AppError(404, 4004, "题目不存在")
    validate_future_due_date(payload.due_date if "due_date" in payload.model_fields_set else None)
    if payload.class_ids is not None:
        validate_class_ids(store, user, payload.class_ids)
    resulting_ids = [pid for pid in assignment["problem_ids"] if pid not in payload.remove_problem_ids]
    resulting_ids.extend(pid for pid in payload.add_problem_ids if pid not in resulting_ids)
    if not resulting_ids:
        raise AppError(422, 4022, "请求参数校验失败", "assignment must contain at least one problem")
    if len(resulting_ids) > 50:
        raise AppError(422, 4022, "请求参数校验失败", "assignment cannot contain more than 50 problems")
    answered_problem_ids = {
        result["problem_id"]
        for submission in store.submissions.values()
        if submission["assignment_id"] == assignment_id
        for result in submission["results"]
    }
    if set(payload.remove_problem_ids) & answered_problem_ids:
        raise AppError(409, 4005, "该题目已有学生提交，不可移除")
    if payload.title is not None:
        assignment["title"] = payload.title
    if "due_date" in payload.model_fields_set:
        assignment["due_date"] = payload.due_date.isoformat() if payload.due_date else None
    if payload.class_ids is not None:
        assignment["class_ids"] = payload.class_ids
    assignment["problem_ids"] = resulting_ids
    return envelope(
        request,
        {
            "assignment_id": assignment_id,
            "title": assignment["title"],
            "due_date": assignment["due_date"],
            "problem_count": len(assignment["problem_ids"]),
        },
    )


async def grade(
    problem: dict,
    raw_answer: str,
    llm_grader: DeepSeekGradingClient,
    hint_level: int = 0,
    rag_indexer: Any | None = None,
) -> dict:
    """Grade through the shared deterministic/LLM-routing pipeline.

    The ``uncertain:`` prefix is an offline development hook used to exercise the
    HITL path without sending data to an external model.
    """
    uncertain = raw_answer.lower().startswith("uncertain:")
    answer = raw_answer.split(":", 1)[1] if uncertain else raw_answer
    type_map = {
        "arithmetic": "calculation",
        "fill_in_blank": "fill_blank",
        "multiple_choice": "choice",
    }
    rag_context: list[dict[str, str]] = []
    if llm_grader.is_enabled and rag_indexer is not None:
        try:
            rag_context = await rag_indexer.search_similar(
                str(problem["tenant_id"]),
                {
                    "problem_id": str(problem.get("problem_id", "")),
                    "problem_text": problem["problem_text"],
                    "reference_answer": problem["reference_answer"],
                    "problem_type": problem["problem_type"],
                    "grade_level": problem["grade_level"],
                    "difficulty": problem.get("difficulty", ""),
                    "tags": problem.get("tags", []),
                },
            )
        except Exception:
            rag_context = []
    grading_request = GradeRequest(
        question=problem["problem_text"],
        reference_answer=problem["reference_answer"],
        student_answer=answer,
        question_type=cast(QuestionType, type_map[problem["problem_type"]]),
        grade=problem["grade_level"],
        hint_level=hint_level,
        solution_steps=problem.get("solution_steps", []),
        rag_context=rag_context,
    )
    llm_verdict: LLMVerdict | None
    if uncertain:
        deterministic = route_grade(grading_request)
        llm_verdict = LLMVerdict(
            is_correct=deterministic.is_correct,
            confidence=0.50,
        )
    elif not llm_grader.is_enabled:
        deterministic = route_grade(grading_request)
        llm_verdict = LLMVerdict(
            is_correct=deterministic.is_correct,
            confidence=0.98,
        )
    else:
        try:
            llm_verdict = await llm_grader.verdict(grading_request)
        except (LLMUnavailableError, ValueError):
            llm_verdict = None
    routed = route_grade(grading_request, llm_verdict)
    if uncertain:
        routed.confidence = 0.50
        routed.needs_review = True
        routed.review_reason = "low_confidence"
    return {
        "student_answer": raw_answer,
        "is_correct": None if routed.needs_review else routed.is_correct,
        "confidence_score": routed.confidence,
        "feedback_text": "老师正在审核这道题。" if routed.needs_review else routed.feedback,
        "encouragement": "真棒！" if routed.is_correct else "继续努力，你可以的！",
        "next_hint": None if routed.is_correct or routed.locked else routed.feedback,
        "error_type": None if routed.is_correct or routed.needs_review else ("未作答" if not answer else "计算错误"),
        "hint_level": hint_level,
        "hint_state": routed.hint_state,
        "locked": routed.locked,
        "show_full_solution": routed.locked,
        "solution_steps": problem.get("solution_steps", []) if routed.locked else [],
        "knowledge_point_recorded": False,
        "attempt_number": hint_level + 1,
        "routed_to_human": routed.needs_review,
        "grading_source": "pending_human_review" if routed.needs_review else routed.source,
        "agent_trace": routed.agent_trace,
    }


def recompute_submission(submission: dict, *, reviewed: bool = False) -> None:
    results = submission["results"]
    correct = sum(item["is_correct"] is True for item in results)
    wrong = sum(item["is_correct"] is False for item in results)
    pending = sum(bool(item["routed_to_human"]) for item in results)
    submission["summary"].update(
        {
            "correct": correct,
            "wrong": wrong,
            "pending_review": pending,
            "accuracy": round(correct / len(results), 3) if results else 0.0,
        }
    )
    submission["status"] = "partial_human_review" if pending else ("reviewed" if reviewed else "graded")
    submission["last_updated_at"] = iso_now()


def add_pending_review(store: Repository, submission: dict, result: dict, user: User, assignment: dict) -> None:
    problem = store.problems[result["problem_id"]]
    review_id = ident()
    store.reviews[review_id] = {
        "review_id": review_id,
        "tenant_id": user.tenant_id,
        "submission_id": submission["submission_id"],
        "problem_id": result["problem_id"],
        "student_name": user.display_name,
        "class_name": "、".join(store.class_name(user.tenant_id, class_id) for class_id in submission["class_ids"]),
        "assignment_title": assignment["title"],
        "problem_text": problem["problem_text"],
        "problem_type": problem["problem_type"],
        "student_answer": result["student_answer"],
        "reference_answer": problem["reference_answer"],
        "ai_conclusion": "待审核",
        "ai_confidence": result["confidence_score"],
        "agent_trace": result.get("agent_trace", []),
        "human_review_reason": "low_confidence",
        "status": "pending",
        "created_at": iso_now(),
    }


@router.post("/submissions/", status_code=201)
async def submit(
    payload: SubmissionCreate,
    request: Request,
    user: User = Depends(require_roles("student")),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
    llm_grader: DeepSeekGradingClient = Depends(get_llm_grader),
):
    if request.app.state.settings.persistence_backend == "postgres":
        data = await repository.submit_assignment(
            user,
            payload.model_dump(),
            lambda problem, answer: grade(problem, answer, llm_grader, rag_indexer=request.app.state.rag_indexer),
        )
        return envelope(request, data)
    assignment = get_assignment(store, payload.assignment_id)
    if not can_access_assignment(user, assignment):
        raise AppError(403, 4003, "该作业不属于你所在的班级")
    if assignment["due_date"] and as_utc(assignment["due_date"]) <= utcnow():
        raise AppError(410, 4006, "作业已截止，无法提交")
    if store.submission_for(user.user_id, payload.assignment_id):
        raise AppError(409, 4005, "该作业已提交，不可重复提交")
    allowed_ids = set(assignment["problem_ids"])
    submitted_ids = [answer.problem_id for answer in payload.answers]
    if len(submitted_ids) != len(set(submitted_ids)):
        raise AppError(422, 4022, "请求参数校验失败", "each problem may be answered only once")
    if any(problem_id not in allowed_ids for problem_id in submitted_ids):
        raise AppError(403, 4003, "题目不属于该作业")
    if set(submitted_ids) != allowed_ids:
        raise AppError(422, 4022, "请求参数校验失败", "answers must cover every assignment problem")
    results = []
    pending = 0
    for answer in payload.answers:
        problem = store.problems[answer.problem_id]
        result = {
            "problem_id": answer.problem_id,
            "sequence": assignment["problem_ids"].index(answer.problem_id) + 1,
            "problem_text": problem["problem_text"],
            **await grade(problem, answer.answer_text, llm_grader, rag_indexer=request.app.state.rag_indexer),
        }
        pending += int(result["routed_to_human"])
        results.append(result)
    correct = sum(result["is_correct"] is True for result in results)
    submission_id = ident()
    status = "partial_human_review" if pending else "graded"
    submitted_at = iso_now()
    submission = {
        "submission_id": submission_id,
        "tenant_id": user.tenant_id,
        "student_id": user.user_id,
        "class_ids": sorted(set(user.class_ids) & set(assignment["class_ids"])),
        "assignment_id": payload.assignment_id,
        "status": status,
        "submitted_at": submitted_at,
        "last_updated_at": submitted_at,
        "results": results,
        "summary": {
            "total": len(results),
            "correct": correct,
            "wrong": len(results) - correct - pending,
            "pending_review": pending,
            "accuracy": round(correct / len(results), 3),
        },
    }
    store.submissions[submission_id] = submission
    store.attempts[submission_id] = {result["problem_id"]: [deepcopy(result)] for result in results}
    store.events[submission_id] = [{"submission_id": submission_id, "status": status}]
    for result in results:
        if result["routed_to_human"]:
            add_pending_review(store, submission, result, user, assignment)
    return envelope(request, public_submission(submission))


@router.get("/submissions/")
async def list_submissions(
    request: Request,
    student_id: str | None = None,
    assignment_id: str | None = None,
    page_number: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(current_user),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        data = await repository.list_submissions(
            user,
            student_id=student_id,
            assignment_id=assignment_id,
            page_number=page_number,
            page_size=page_size,
        )
        return envelope(request, data)
    if user.role == "student":
        student_id = user.user_id
    elif user.role not in {"teacher", "admin", "sysadmin"}:
        raise AppError(403, 4003, "权限不足")
    items = []
    for submission in store.submissions.values():
        if (
            submission["tenant_id"] != user.tenant_id
            or (student_id and submission["student_id"] != student_id)
            or (assignment_id and submission["assignment_id"] != assignment_id)
        ):
            continue
        if user.role == "teacher" and not can_access_submission(user, submission):
            continue
        public = public_submission(submission)
        public.pop("results", None)
        items.append(public)
    return envelope(request, page(items, page_number, page_size))


@router.get("/submissions/{submission_id}")
async def submission_detail(
    submission_id: str,
    request: Request,
    user: User = Depends(current_user),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        return envelope(request, await repository.submission_detail(user, submission_id))
    return envelope(request, public_submission(get_visible_submission(store, submission_id, user)))


@router.post("/submissions/{submission_id}/hint")
async def hint(
    submission_id: str,
    payload: HintRequest,
    request: Request,
    user: User = Depends(require_roles("student")),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
    llm_grader: DeepSeekGradingClient = Depends(get_llm_grader),
):
    if request.app.state.settings.persistence_backend == "postgres":
        data = await repository.request_hint(
            user,
            submission_id,
            payload.model_dump(),
            lambda problem, answer, hint_level: grade(
                problem, answer, llm_grader, hint_level, request.app.state.rag_indexer
            ),
        )
        return envelope(request, data)
    submission = get_visible_submission(store, submission_id, user)
    assignment = store.assignments[submission["assignment_id"]]
    if assignment["due_date"] and as_utc(assignment["due_date"]) <= utcnow():
        raise AppError(410, 4006, "作业已截止，无法继续尝试")
    result = next((item for item in submission["results"] if item["problem_id"] == payload.problem_id), None)
    if result is None:
        raise AppError(403, 4003, "这道题不属于你的提交记录")
    if result["is_correct"] is True:
        raise AppError(409, 4007, "该题已经答对，无需继续提交")
    if result["hint_level"] >= 3:
        raise AppError(409, 4007, "该题已展示完整解法，无法继续提交")
    problem = store.problems[payload.problem_id]
    previous_hint_level = result["hint_level"]
    previous_attempt_number = result["attempt_number"]
    for review in store.reviews.values():
        if (
            review["submission_id"] == submission_id
            and review["problem_id"] == payload.problem_id
            and review["status"] == "pending"
        ):
            review["status"] = "reviewed"
            review["resolution"] = "superseded"
            review["superseded_at"] = iso_now()
    result.update(
        await grade(
            problem,
            payload.new_answer.strip(),
            llm_grader,
            previous_hint_level + 1,
            request.app.state.rag_indexer,
        )
    )
    result["hint_level"] = previous_hint_level + 1
    result["attempt_number"] = previous_attempt_number + 1
    if result["show_full_solution"]:
        record_id = ident()
        record = {
            "record_id": record_id,
            "record_type": "error_history",
            "tenant_id": user.tenant_id,
            "student_id": user.user_id,
            "submission_id": submission_id,
            "assignment_id": submission["assignment_id"],
            "problem_id": payload.problem_id,
            "knowledge_points": list(problem.get("tags", [])),
            "error_type": result["error_type"],
            "student_answer": result["student_answer"],
            "created_at": iso_now(),
        }
        store.knowledge_records[record_id] = record
        result["knowledge_point_recorded"] = True
        result["knowledge_point_record"] = record
    store.attempts[submission_id][payload.problem_id].append(deepcopy(result))
    if result["routed_to_human"]:
        add_pending_review(store, submission, result, user, assignment)
    recompute_submission(submission)
    store.events[submission_id].append(
        {
            "submission_id": submission_id,
            "problem_id": payload.problem_id,
            "status": submission["status"],
            "is_correct": result["is_correct"],
            "routed_to_human": result["routed_to_human"],
        }
    )
    data = {
        key: result[key]
        for key in (
            "problem_id",
            "student_answer",
            "is_correct",
            "hint_level",
            "attempt_number",
            "feedback_text",
            "encouragement",
            "next_hint",
            "hint_state",
            "locked",
            "show_full_solution",
            "solution_steps",
            "knowledge_point_recorded",
            "routed_to_human",
            "confidence_score",
        )
    }
    if "knowledge_point_record" in result:
        data["knowledge_point_record"] = result["knowledge_point_record"]
    data["remaining_hints"] = 3 - result["hint_level"]
    return envelope(request, data)


@router.get("/teacher/dashboard")
async def teacher_dashboard(
    request: Request,
    class_id: str | None = None,
    assignment_id: str | None = None,
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(
        request,
        await repository.teacher_dashboard(user, class_id=class_id, assignment_id=assignment_id, days=days),
    )


@router.get("/teacher/students/{student_id}/analytics")
async def student_analytics(
    student_id: str,
    request: Request,
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.student_analytics(user, student_id, days=days))


@router.get("/teacher/export/assignment/{assignment_id}")
async def export_assignment(
    assignment_id: str,
    format: Literal["excel"] = "excel",
    user: User = Depends(require_roles("teacher", "admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    report = await repository.assignment_export(user, assignment_id)
    filename = assignment_export_filename(assignment_id)
    return Response(
        content=build_assignment_report_xlsx(report),
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/teacher/human-review-queue")
async def review_queue(
    request: Request,
    response: Response,
    status: Literal["pending", "reviewed", "all"] = "pending",
    class_id: str | None = None,
    page_number: int = Query(1, alias="page", ge=1),
    page_size: int = Query(20, ge=1, le=100),
    user: User = Depends(require_roles("teacher", "admin")),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        data, pending_count = await repository.list_human_reviews(
            user,
            status=status,
            class_id=class_id,
            page_number=page_number,
            page_size=page_size,
        )
        response.headers["X-Pending-Review-Count"] = str(pending_count)
        return envelope(request, data)
    if class_id is not None:
        if class_id not in store.known_class_ids(user.tenant_id):
            raise AppError(404, 4004, "班级不存在")
        if user.role == "teacher" and class_id not in user.class_ids:
            raise AppError(403, 4003, "权限不足")
    visible_reviews = []
    for review in store.reviews.values():
        submission = store.submissions[review["submission_id"]]
        if review["tenant_id"] != user.tenant_id or (
            user.role == "teacher" and not can_access_submission(user, submission)
        ):
            continue
        if class_id is not None and class_id not in submission["class_ids"]:
            continue
        visible_reviews.append(review)
    reviews = [review for review in visible_reviews if status == "all" or review["status"] == status]
    response.headers["X-Pending-Review-Count"] = str(sum(review["status"] == "pending" for review in visible_reviews))
    return envelope(request, page(reviews, page_number, page_size))


@router.get("/teacher/human-review/{review_id}")
async def review_detail(
    review_id: str,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        return envelope(request, await repository.human_review_detail(user, review_id))
    review = store.reviews.get(review_id)
    if not review or review["tenant_id"] != user.tenant_id:
        raise AppError(404, 4004, "审核记录不存在")
    submission = store.submissions[review["submission_id"]]
    if user.role == "teacher" and not can_access_submission(user, submission):
        raise AppError(404, 4004, "审核记录不存在")
    return envelope(request, review)


@router.post("/teacher/human-review/{review_id}")
async def review_override(
    review_id: str,
    payload: ReviewRequest,
    request: Request,
    user: User = Depends(require_roles("teacher", "admin")),
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if request.app.state.settings.persistence_backend == "postgres":
        return envelope(request, await repository.resolve_human_review(user, review_id, payload.model_dump()))
    review = store.reviews.get(review_id)
    if not review or review["status"] != "pending" or review["tenant_id"] != user.tenant_id:
        raise AppError(404, 4004, "待审核记录不存在")
    submission = store.submissions[review["submission_id"]]
    if user.role == "teacher" and not can_access_submission(user, submission):
        raise AppError(404, 4004, "待审核记录不存在")
    result = next(item for item in submission["results"] if item["problem_id"] == review["problem_id"])
    result.update(
        {
            "is_correct": payload.override_correct,
            "error_type": None if payload.override_correct else payload.override_error_type,
            "feedback_text": payload.override_feedback or result["feedback_text"] + "（已经过老师审核）",
            "routed_to_human": False,
            "grading_source": "human_override",
            "confidence_score": 1.0,
        }
    )
    review.update(
        {
            "status": "reviewed",
            "reviewed_by": user.user_id,
            "reviewer_notes": payload.reviewer_notes,
            "is_training_example": payload.is_training_example,
        }
    )
    recompute_submission(submission, reviewed=True)
    store.events[submission["submission_id"]].append(
        {
            "submission_id": submission["submission_id"],
            "problem_id": result["problem_id"],
            "routed_to_human": False,
            "is_correct": payload.override_correct,
            "feedback_text": result["feedback_text"],
        }
    )
    return envelope(
        request,
        {
            "review_id": review_id,
            "status": "reviewed",
            "override_correct": payload.override_correct,
            "student_notified": True,
            "notify_eta_seconds": 0,
            "is_training_example": payload.is_training_example,
        },
    )


@router.post("/admin/classes/", status_code=201)
async def create_class(
    payload: ClassCreate,
    request: Request,
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.create_class(user, payload.model_dump()))


@router.delete("/admin/classes/{class_id}")
async def delete_class(
    class_id: str,
    request: Request,
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.delete_class(user, class_id))


@router.post("/admin/students/bulk-create")
async def bulk_create_students(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    try:
        imported_rows = parse_student_import(file.filename or "students.csv", await file.read())
    except (UnicodeDecodeError, ValueError) as exc:
        raise AppError(422, 4022, "请求参数校验失败", str(exc)) from exc
    rows = []
    for row in imported_rows:
        if len(row.username) > 100 or any(character.isspace() for character in row.username):
            raise AppError(
                422, 4022, "请求参数校验失败", f"row {row.row_number}: username must be 1-100 non-space chars"
            )
        if len(row.display_name) > 100:
            raise AppError(
                422, 4022, "请求参数校验失败", f"row {row.row_number}: display_name must be at most 100 chars"
            )
        if len(row.initial_password) < 6 or len(row.initial_password) > 128:
            raise AppError(422, 4022, "请求参数校验失败", f"row {row.row_number}: initial_password must be 6-128 chars")
        rows.append(
            {
                "row": row.row_number,
                "display_name": row.display_name,
                "username": row.username,
                "password_hash": hash_password(row.initial_password, request.app.state.settings),
                "grade_level": row.grade_level,
                "class_name": row.class_name,
            }
        )
    return envelope(request, await repository.bulk_create_students(user, rows))


@router.post("/admin/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    payload: AdminResetPasswordRequest,
    request: Request,
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    if (
        len(payload.new_password) < 8
        or not any(character.isalpha() for character in payload.new_password)
        or not any(character.isdigit() for character in payload.new_password)
    ):
        raise AppError(422, 4022, "请求参数校验失败", "Admin reset password requires letters and digits")
    data = await repository.reset_user_password(
        user, user_id, hash_password(payload.new_password, request.app.state.settings)
    )
    return envelope(request, data)


@router.patch("/admin/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    payload: AdminUserStatusRequest,
    request: Request,
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.update_user_status(user, user_id, payload.is_active))


@router.get("/admin/stats/overview")
async def admin_stats_overview(
    request: Request,
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.admin_stats_overview(user))


@router.post("/ops/harness/run", status_code=202)
async def run_harness(
    payload: HarnessRunRequest,
    request: Request,
    user: User = Depends(require_roles("sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
    llm_grader: DeepSeekGradingClient = Depends(get_llm_grader),
):
    if not payload.use_mock and not llm_grader.is_enabled:
        raise AppError(503, 5002, "LLM 服务不可用", "请先配置 LLM 再运行真实 Harness")
    report = (
        await HarnessRunner(use_mock=payload.use_mock).run_async(
            HARNESS_DATASET,
            sample_rate=payload.sample_rate,
            grade_levels=payload.grade_levels,
            llm_grader=llm_grader,
        )
    ).as_dict()
    return envelope(request, await repository.run_harness(user, payload.model_dump(), report))


@router.get("/ops/harness/runs/{run_id}")
async def harness_run_detail(
    run_id: str,
    request: Request,
    user: User = Depends(require_roles("sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.harness_run_detail(user, run_id))


@router.post("/ops/rag/ingest", status_code=202)
async def rag_ingest(
    payload: RagIngestRequest,
    request: Request,
    user: User = Depends(require_roles("sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(
        request, await repository.create_rag_ingest_job(user, payload.model_dump(), request.app.state.rag_indexer)
    )


@router.get("/ops/jobs/{job_id}")
async def job_detail(
    job_id: str,
    request: Request,
    user: User = Depends(require_roles("admin", "sysadmin")),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    return envelope(request, await repository.job_detail(user, job_id))


@router.get("/submissions/{submission_id}/events")
async def submission_events(
    submission_id: str,
    request: Request,
    sse_ticket: str,
    follow: bool = True,
    store: Repository = Depends(get_store),
    repository: IdentityProblemRepository = Depends(get_identity_repository),
):
    ticket = await request.app.state.ticket_repository.consume(sse_ticket)
    if ticket is None or ticket.submission_id != submission_id:
        raise AppError(401, 4001, "SSE 票据无效或已过期")
    if request.app.state.settings.persistence_backend == "postgres":
        return StreamingResponse(
            postgres_sse_event_stream(repository, ticket, follow=follow),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    submission = store.submissions.get(submission_id)
    if submission is None or submission["tenant_id"] != ticket.tenant_id:
        raise AppError(404, 4004, "提交记录不存在")

    events = store.events.get(submission_id) or [{"submission_id": submission_id, "status": submission["status"]}]
    return StreamingResponse(
        sse_event_stream(events, heartbeat_count=1, follow=follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
