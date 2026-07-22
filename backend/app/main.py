from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
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
from app.db.session import tenant_conn, tenant_context
from app.domain.memory import InMemoryRepository
from app.domain.postgres import NIL_SYSTEM_USER_ID, PostgresIdentityProblemRepository
from app.grading import DeepSeekGradingClient, LLMClientConfig
from app.rag import QdrantIndexer, QdrantIndexerConfig
from app.realtime import MemoryTicketRepository, RedisTicketRepository, TicketRepository

PoolFactory = Callable[[str], Awaitable[asyncpg.Pool]]
RedisFactory = Callable[[str], Redis]
QdrantIndexerFactory = Callable[[Settings], QdrantIndexer | None]


def service_health(status: str, *, latency_ms: int | None = None, backend: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "latency_ms": latency_ms}
    if backend is not None:
        payload["backend"] = backend
    return payload


async def pending_hitl_count(request: Request, pool: asyncpg.Pool | None) -> int:
    store = request.app.state.store
    if store is not None:
        return sum(review["status"] == "pending" for review in store.reviews.values())
    if pool is None:
        return 0
    try:
        configured: Settings = request.app.state.settings
        with tenant_context(configured.default_tenant_id or "", "worker"):
            async with tenant_conn(pool, user_id=NIL_SYSTEM_USER_ID) as connection:
                count = await connection.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM human_review_queue
                    WHERE status = 'pending'
                    """
                )
    except Exception:
        return 0
    return int(count or 0)


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
    application.state.started_at = monotonic()
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
        services: dict[str, dict[str, Any]] = {}
        status_code = 200
        if pool is not None:
            database_started = monotonic()
            try:
                async with pool.acquire() as connection:
                    await connection.fetchval("SELECT 1")
                services["database"] = service_health("ok", latency_ms=round((monotonic() - database_started) * 1000))
            except Exception:  # health endpoint must report dependency failure without leaking details
                services["database"] = service_health("unavailable")
                status_code = 503
        else:
            services["database"] = service_health("not-wired", backend=configured.persistence_backend)
        repository_status = (
            "hybrid-postgres-identity-problems-assignments"
            if configured.persistence_backend == "postgres"
            else "development-in-memory-adapter"
        )
        services["repository"] = service_health("ok", backend=repository_status)
        ticket_repository: TicketRepository = request.app.state.ticket_repository
        ticket_started = monotonic()
        tickets_ok = await ticket_repository.ping()
        ticket_latency = round((monotonic() - ticket_started) * 1000) if tickets_ok else None
        services["sse_tickets"] = service_health(
            "ok" if tickets_ok else "unavailable",
            latency_ms=ticket_latency,
            backend=ticket_repository.backend_name,
        )
        if not tickets_ok:
            status_code = 503
        if configured.redis_url:
            services["redis"] = service_health(
                "ok" if tickets_ok else "unavailable",
                latency_ms=ticket_latency if tickets_ok else None,
                backend="redis",
            )
        else:
            services["redis"] = service_health("not-wired")
        rag_indexer = request.app.state.rag_indexer
        services["qdrant"] = service_health(
            "ok" if rag_indexer is not None else "not-wired",
            backend=rag_indexer.status if rag_indexer is not None else "local-metadata-index",
        )
        llm_status = request.app.state.llm_grader.health_status
        services["deepseek"] = service_health("ok" if llm_status in {"mock", "configured"} else llm_status)
        unavailable = {"unavailable"}
        degraded = {"not-wired", "unconfigured"}
        overall = "ok"
        if any(service["status"] in unavailable for service in services.values()):
            overall = "degraded"
        elif any(service["status"] in degraded for service in services.values()):
            overall = "degraded"
        body = {
            "status": overall,
            "version": request.app.version,
            "environment": configured.app_env,
            "uptime_seconds": round(monotonic() - request.app.state.started_at),
            "services": services,
            "grading": {"active_requests": 0, "pending_hitl_count": await pending_hitl_count(request, pool)},
        }
        return JSONResponse(status_code=status_code, content=body)

    application.include_router(router, prefix=configured.api_prefix)
    return application


app = create_app(settings)
