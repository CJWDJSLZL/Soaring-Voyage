from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router
from app.config import Settings, settings
from app.core.errors import AppError, trace_id
from app.db.pool import create_pool
from app.domain.memory import InMemoryRepository
from app.domain.postgres import PostgresIdentityProblemRepository

PoolFactory = Callable[[str], Awaitable[asyncpg.Pool]]


def create_app(configured: Settings, *, pool_factory: PoolFactory = create_pool) -> FastAPI:
    """Build an application with an explicit persistence adapter lifecycle."""
    store = InMemoryRepository()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = configured
        application.state.store = store
        application.state.pool = None
        application.state.identity_repository = store
        if configured.persistence_backend == "postgres":
            pool = await pool_factory(configured.database_url or "")
            application.state.pool = pool
            application.state.identity_repository = PostgresIdentityProblemRepository(
                pool, configured.default_tenant_id or ""
            )
        try:
            yield
        finally:
            pool = application.state.pool
            if pool is not None:
                await pool.close()

    application = FastAPI(title="翱翔启航 API", version="1.0.0", lifespan=lifespan)
    # Preserve the established test/tooling contract that exposes the memory
    # adapter before lifespan startup; external resources are still opened only
    # by lifespan.
    application.state.settings = configured
    application.state.store = store
    application.state.pool = None
    application.state.identity_repository = store
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=list(configured.allowed_hosts))
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Trace-ID"],
    )

    @application.middleware("http")
    async def request_trace(request: Request, call_next: Callable[..., Awaitable[Any]]):
        supplied = request.headers.get("X-Trace-ID", "")
        request.state.trace_id = supplied if supplied.startswith("req-") else f"req-{uuid4()}"
        path = request.url.path
        unported_prefixes = (
            f"{configured.api_prefix}/submissions",
            f"{configured.api_prefix}/teacher/human-review",
        )
        unported_exact = {f"{configured.api_prefix}/auth/sse-ticket"}
        if configured.persistence_backend == "postgres" and (
            path.startswith(unported_prefixes) or path in unported_exact
        ):
            response = JSONResponse(
                status_code=503,
                content={
                    "code": 5003,
                    "message": "该工作流尚未迁移到 PostgreSQL",
                    "detail": "当前 PostgreSQL 阶段仅支持认证和题库接口",
                    "trace_id": request.state.trace_id,
                },
            )
        else:
            response = await call_next(request)
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response

    @application.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        body = {"code": exc.code, "message": exc.message, "trace_id": trace_id(request)}
        if exc.detail is not None:
            body["detail"] = exc.detail
        body.update(exc.extra)
        return JSONResponse(status_code=exc.status_code, content=body)

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        errors = []
        for error in exc.errors():
            item = {key: value for key, value in error.items() if key not in {"input", "ctx"}}
            if "ctx" in error:
                item["ctx"] = {key: str(value) for key, value in error["ctx"].items()}
            errors.append(item)
        return JSONResponse(
            status_code=422,
            content={"code": 4022, "message": "请求参数校验失败", "detail": errors, "trace_id": trace_id(request)},
        )

    @application.get("/health")
    async def health(request: Request):
        pool = request.app.state.pool
        database_status = "not-wired"
        status_code = 200
        if pool is not None:
            try:
                async with pool.acquire() as connection:
                    await connection.fetchval("SELECT 1")
                database_status = "ok"
            except Exception:  # health endpoint must report dependency failure without leaking details
                database_status = "unavailable"
                status_code = 503
        repository_status = (
            "hybrid-postgres-identity-problems-assignments"
            if configured.persistence_backend == "postgres"
            else "development-in-memory-adapter"
        )
        body = {
            "status": "degraded",
            "environment": configured.app_env,
            "services": {
                "repository": repository_status,
                "sse_tickets": "development-in-memory-adapter",
                "database": database_status,
                "redis": "not-wired",
                "qdrant": "not-wired",
            },
        }
        return JSONResponse(status_code=status_code, content=body)

    application.include_router(router, prefix=configured.api_prefix)
    return application


app = create_app(settings)
