from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.routes import router
from app.config import settings
from app.core.errors import AppError, trace_id
from app.domain.memory import InMemoryRepository

app = FastAPI(title="翱翔启航 API", version="1.0.0")
app.state.store = InMemoryRepository()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.allowed_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Trace-ID"],
)


@app.middleware("http")
async def request_trace(request: Request, call_next):
    supplied = request.headers.get("X-Trace-ID", "")
    request.state.trace_id = supplied if supplied.startswith("req-") else f"req-{uuid4()}"
    response = await call_next(request)
    response.headers["X-Trace-ID"] = request.state.trace_id
    return response


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    body = {"code": exc.code, "message": exc.message, "trace_id": trace_id(request)}
    if exc.detail is not None:
        body["detail"] = exc.detail
    body.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        item = {key: value for key, value in error.items() if key != "input" and key != "ctx"}
        if "ctx" in error:
            item["ctx"] = {key: str(value) for key, value in error["ctx"].items()}
        errors.append(item)
    return JSONResponse(
        status_code=422,
        content={"code": 4022, "message": "请求参数校验失败", "detail": errors, "trace_id": trace_id(request)},
    )


@app.get("/health")
def health():
    return {
        "status": "degraded",
        "environment": settings.app_env,
        "services": {
            "repository": "development-in-memory-adapter",
            "sse_tickets": "development-in-memory-adapter",
            "database": "not-wired",
            "redis": "not-wired",
            "qdrant": "not-wired",
        },
    }


app.include_router(router, prefix=settings.api_prefix)
