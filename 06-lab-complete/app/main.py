"""
Production AI Agent — Part 6 Final Project

  GET  /health  liveness  — luôn 200 khi process còn sống (không phụ thuộc Redis)
  GET  /ready   readiness — 200 khi Redis kết nối được và chưa nhận SIGTERM, ngược lại 503
  POST /ask     X-API-Key → rate limit (Redis) → cost guard (Redis) → mock LLM

Kiểm tra: python check_production_ready.py
"""
import json
import logging
import signal
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.auth import verify_api_key
from app.config import settings
from app.cost_guard import check_budget, estimate_tokens, record_cost
from app.rate_limiter import check_rate_limit
from app.redis_client import r, redis_healthy
from utils.mock_llm import ask as llm_ask


# ─────────────────────────────────────────────────────────
# Structured logging: mỗi dòng log là 1 JSON object
# ─────────────────────────────────────────────────────────
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        # Các field truyền qua extra={...}
        for k, v in vars(record).items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(payload, ensure_ascii=False, default=str)


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)


_setup_logging()
logger = logging.getLogger("agent")


# ─────────────────────────────────────────────────────────
# Graceful shutdown
#   Uvicorn đã tự xử lý SIGTERM: ngừng nhận kết nối mới, chờ request đang chạy xong,
#   chạy lifespan shutdown rồi thoát exit code 0. Ta chỉ bọc thêm handler để /ready
#   trả 503 ngay khi nhận SIGTERM (load balancer ngừng gửi traffic), rồi GỌI LẠI handler
#   cũ của uvicorn — không gọi lại thì process không bao giờ tắt.
# ─────────────────────────────────────────────────────────
STATE = {"shutting_down": False, "in_flight": 0}


def _install_sigterm_hook() -> None:
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (ValueError, AttributeError):
        return

    def _on_sigterm(signum, frame):
        STATE["shutting_down"] = True
        logger.info("sigterm_received", extra={"in_flight": STATE["in_flight"]})
        if callable(previous):
            previous(signum, frame)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # không ở main thread
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _install_sigterm_hook()
    logger.info("startup", extra={"environment": settings.environment, "port": settings.port,
                                  "rate_limit_per_minute": settings.rate_limit_per_minute,
                                  "redis_ok": redis_healthy()})
    yield
    # Tới đây uvicorn đã chờ xong mọi request đang chạy
    STATE["shutting_down"] = True
    try:
        r.close()
    except Exception:
        pass
    logger.info("shutdown_complete")


is_prod = settings.environment == "production"
app = FastAPI(
    title="Production AI Agent",
    lifespan=lifespan,
    docs_url=None if is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if is_prod else "/openapi.json",
)


# ─────────────────────────────────────────────────────────
# Middleware: request id + log mỗi request + đếm in-flight
# ─────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    start = time.perf_counter()
    STATE["in_flight"] += 1
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        STATE["in_flight"] -= 1
        logger.info("request", extra={
            "request_id": request_id, "method": request.method, "path": request.url.path,
            "status": status, "ms": round((time.perf_counter() - start) * 1000, 1),
        })


# ─────────────────────────────────────────────────────────
# Error handling: mã lỗi rõ ràng, không lộ stack trace
# ─────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    errors = [{"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "msg": e["msg"]}
              for e in exc.errors()]
    return JSONResponse(status_code=422, content={"error": "invalid_request", "details": errors})


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    logger.error("unhandled_error", exc_info=exc, extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"error": "internal_error"})


# ─────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    user_id: str | None = Field(default=None, max_length=128)


@app.get("/")
def root():
    return {"service": "production-ai-agent", "endpoints": ["/health", "/ready", "POST /ask"]}


@app.get("/health")
def health():
    """Liveness: process còn sống là 200 — KHÔNG kiểm tra Redis (Redis chết không nên restart app)."""
    return {"status": "ok", "environment": settings.environment}


@app.get("/ready")
def ready():
    """Readiness: chỉ nhận traffic khi Redis OK và chưa nhận SIGTERM."""
    if STATE["shutting_down"]:
        raise HTTPException(status_code=503, detail="shutting down")
    if not redis_healthy():
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"status": "ready"}


@app.post("/ask")
def ask(body: AskRequest, api_key: str = Depends(verify_api_key)):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="question must not be blank")

    check_rate_limit(api_key)   # 429
    check_budget()              # 402

    answer = llm_ask(question)

    input_tokens, output_tokens = estimate_tokens(question), estimate_tokens(answer)
    try:
        cost = record_cost(input_tokens, output_tokens)
    except redis.RedisError:
        cost = None
        logger.warning("record_cost_failed")

    logger.info("llm_call", extra={"input_tokens": input_tokens, "output_tokens": output_tokens,
                                   "cost_usd": cost, "user_id": body.user_id})
    return {
        "question": question,
        "answer": answer,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost},
    }
