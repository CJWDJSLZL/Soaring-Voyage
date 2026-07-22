from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router
from app.config import Settings, settings
from app.core.errors import AppError, trace_id
from app.db.pool import create_pool
from app.domain.memory import InMemoryRepository
from app.domain.postgres import PostgresIdentityProblemRepository
from app.grading import DeepSeekGradingClient, LLMClientConfig
from app.rag import QdrantIndexer, QdrantIndexerConfig
from app.realtime import MemoryTicketRepository, RedisTicketRepository, TicketRepository

PoolFactory = Callable[[str], Awaitable[asyncpg.Pool]]
RedisFactory = Callable[[str], Redis]
QdrantIndexerFactory = Callable[[Settings], QdrantIndexer | None]


def create_redis_client(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, decode_responses=True)


def create_qdrant_indexer(configured: Settings) -> QdrantIndexer | None:
    if not configured.qdrant_url:
        return None
    return QdrantIndexer(
        QdrantIndexerConfig(
            url=configured.qdrant_url,
            collection=configured.qdrant_collection,
            vector_size=configured.qdrant_vector_size,
        )
    )


def create_app(
    configured: Settings,
    *,
    pool_factory: PoolFactory = create_pool,
    redis_factory: RedisFactory = create_redis_client,
    qdrant_indexer_factory: QdrantIndexerFactory = create_qdrant_indexer,
) -> FastAPI:
    """Build an application with an explicit persistence adapter lifecycle."""
    store = InMemoryRepository() if configured.is_development else None

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = configured
        application.state.store = store
        application.state.pool = None
        application.state.redis_client = None
        application.state.rag_indexer = qdrant_indexer_factory(configured)
        application.state.identity_repository = store
        application.state.ticket_repository = MemoryTicketRepository(store.tickets) if store is not None else None
        application.state.llm_grader = DeepSeekGradingClient(LLMClientConfig.from_settings(configured))
        if configured.persistence_backend == "postgres":
            pool = await pool_factory(configured.database_url or "")
            application.state.pool = pool
            application.state.identity_repository = PostgresIdentityProblemRepository(
                pool, configured.default_tenant_id or ""
            )
        if configured.redis_url:
            redis_client = redis_factory(configured.redis_url)
            application.state.redis_client = redis_client
            application.state.ticket_repository = RedisTicketRepository(redis_client)
        try:
            yield
        finally:
            pool = application.state.pool
            if pool is not None:
                await pool.close()
            ticket_repository: TicketRepository = application.state.ticket_repository
            if ticket_repository is not None:
                await ticket_repository.close()
            rag_indexer = application.state.rag_indexer
            if rag_indexer is not None:
                await rag_indexer.close()
            await application.state.llm_grader.close()

    application = FastAPI(title="翱翔启航 API", version="1.0.0", lifespan=lifespan)
    # Preserve the established test/tooling contract that exposes the memory
    # adapter before lifespan startup; external resources are still opened only
    # by lifespan.
    application.state.settings = configured
    application.state.store = store
    application.state.pool = None
    application.state.redis_client = None
    application.state.rag_indexer = None
    application.state.identity_repository = store
    application.state.ticket_repository = MemoryTicketRepository(store.tickets) if store is not None else None
    application.state.llm_grader = DeepSeekGradingClient(LLMClientConfig.from_settings(configured))
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
        ticket_repository: TicketRepository = request.app.state.ticket_repository
        tickets_ok = await ticket_repository.ping()
        ticket_status = ticket_repository.backend_name if tickets_ok else "unavailable"
        if not tickets_ok:
            status_code = 503
        body = {
            "status": "degraded",
            "environment": configured.app_env,
            "services": {
                "repository": repository_status,
                "sse_tickets": ticket_status,
                "database": database_status,
                "redis": "ok"
                if configured.redis_url and tickets_ok
                else "unavailable"
                if configured.redis_url
                else "not-wired",
                "qdrant": request.app.state.rag_indexer.status
                if request.app.state.rag_indexer is not None
                else "local-metadata-index",
                "llm": request.app.state.llm_grader.health_status,
            },
        }
        return JSONResponse(status_code=status_code, content=body)

    application.include_router(router, prefix=configured.api_prefix)
    return application


app = create_app(settings)
