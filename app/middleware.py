from __future__ import annotations

import hashlib
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass

from fastapi.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.db.database import SessionLocal
from app.models.rate_limit import RateLimitBucket


logger = logging.getLogger("dinebook.http")
ALLOWED_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
RATE_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateLimit:
    name: str
    limit: int


def rate_limit_for(path: str, method: str) -> RateLimit | None:
    """Select conservative per-IP limits for API routes, with tighter auth caps."""
    if not path.startswith("/api/") or path in {"/api/health", "/api/webhooks/razorpay"}:
        return None
    if path.startswith("/api/events/"):
        return None
    if method == "OPTIONS":
        return None
    if path.startswith("/api/auth/") and method == "POST":
        if path.endswith("/login"):
            return RateLimit("auth-login", 10)
        if path.endswith("/forgot-password") or path.endswith("/reset-password"):
            return RateLimit("auth-recovery", 6)
        return RateLimit("auth-write", 20)
    if method in BODY_METHODS and (
        "/payment/" in path or "/topups" in path or path == "/api/customer/reservations"
    ):
        return RateLimit("booking-payment", 30)
    if path == "/api/customer/restaurants/nearby" and method == "POST":
        return RateLimit("restaurant-search", 120)
    if path.startswith("/api/customer/restaurants") and method in {"GET", "HEAD"}:
        return RateLimit("restaurant-browse", 180)
    if method in BODY_METHODS:
        return RateLimit("api-write", 180)
    return RateLimit("api-read", 600)


def _client_address(request: Request) -> str:
    # Trust the ASGI peer address. Forwarded headers require an explicitly
    # trusted proxy configuration and are deliberately not accepted here.
    return request.client.host if request.client else "unknown"


def _increment_bucket(bucket_key: str, window_start: int) -> int:
    with SessionLocal() as db:
        statement = insert(RateLimitBucket).values(
            bucket_key=bucket_key,
            window_start=window_start,
            request_count=1,
        ).on_conflict_do_update(
            index_elements=[RateLimitBucket.bucket_key, RateLimitBucket.window_start],
            set_={"request_count": RateLimitBucket.request_count + 1},
        ).returning(RateLimitBucket.request_count)
        count = db.execute(statement).scalar_one()
        if random.randrange(600) == 0:
            db.execute(delete(RateLimitBucket).where(RateLimitBucket.window_start < window_start - 86_400))
        db.commit()
        return count


def _route_name(scope: dict) -> str:
    route = scope.get("route")
    return getattr(route, "path", "unmatched")


def _write_request_log(request_id: str, method: str, route: str, status_code: int, duration_ms: float) -> None:
    logger.info(json.dumps({
        "event": "http_request",
        "request_id": request_id,
        "method": method,
        "route": route,
        "status_code": status_code,
        "duration_ms": round(duration_ms, 2),
    }, separators=(",", ":")))


class RequestGuardMiddleware:
    """Bound request bodies, reject unsupported methods, rate limit, and log requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        request_id = str(uuid.uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        method = request.method.upper()
        path = request.url.path
        status_code = 500
        response_started = False

        async def tracked_send(message):
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                status_code = message["status"]
                response_started = True
            await send(message)

        async def reject(code: int, detail: str, headers: dict[str, str] | None = None):
            duration_ms = (time.perf_counter() - started) * 1000
            response = JSONResponse(
                {"detail": detail, "request_id": request_id},
                status_code=code,
                headers={
                    "X-Request-ID": request_id,
                    "X-Process-Time-Ms": f"{duration_ms:.2f}",
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "strict-origin-when-cross-origin",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                    "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                    "Cache-Control": "no-store",
                    **(headers or {}),
                },
            )
            await response(scope, receive, tracked_send)

        if method not in ALLOWED_METHODS:
            await reject(405, "HTTP method is not supported.", {"Allow": ", ".join(sorted(ALLOWED_METHODS))})
            _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
            return

        limit = rate_limit_for(path, method)
        count = 0
        if settings.rate_limit_enabled and limit is not None:
            client_address = _client_address(request)
            bucket_hash = hashlib.sha256(f"{limit.name}:{client_address}".encode()).hexdigest()
            window_start = int(time.time()) // RATE_WINDOW_SECONDS * RATE_WINDOW_SECONDS
            try:
                count = await run_in_threadpool(_increment_bucket, bucket_hash, window_start)
            except Exception:
                logger.exception("Rate-limit store unavailable; request rejected. request_id=%s", request_id)
                await reject(503, "Request protection is temporarily unavailable.")
                _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
                return
            if count > limit.limit:
                retry_after = max(1, window_start + RATE_WINDOW_SECONDS - int(time.time()))
                headers = {
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit.limit),
                    "X-RateLimit-Remaining": "0",
                }
                await reject(429, "Too many requests. Please retry later.", headers)
                _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
                return

        if method in BODY_METHODS:
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            body_limit = settings.max_request_body_bytes if content_type == "multipart/form-data" else settings.max_json_body_bytes
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    await reject(400, "Invalid Content-Length header.")
                    _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
                    return
                if declared_size < 0:
                    await reject(400, "Invalid Content-Length header.")
                    _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
                    return
                if declared_size > body_limit:
                    await reject(413, "Request body is too large.")
                    _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
                    return

            body_parts: list[bytes] = []
            received_size = 0
            disconnected = False
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected = True
                    break
                if message["type"] != "http.request":
                    continue
                part = message.get("body", b"")
                received_size += len(part)
                if received_size > body_limit:
                    await reject(413, "Request body is too large.")
                    _write_request_log(request_id, method, "unmatched", status_code, (time.perf_counter() - started) * 1000)
                    return
                body_parts.append(part)
                if not message.get("more_body", False):
                    break
            if disconnected:
                return

            body = b"".join(body_parts)
            replayed = False

            async def bounded_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            receive_for_app = bounded_receive
        else:
            receive_for_app = receive

        async def rate_limit_headers_send(message):
            if message["type"] == "http.response.start" and settings.rate_limit_enabled and limit is not None:
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"x-ratelimit-limit", str(limit.limit).encode()),
                    (b"x-ratelimit-remaining", str(max(0, limit.limit - count)).encode()),
                ])
                message["headers"] = headers
            await tracked_send(message)

        try:
            await self.app(scope, receive_for_app, rate_limit_headers_send)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception("Unhandled API error. request_id=%s method=%s route=%s", request_id, method, _route_name(scope))
            if response_started:
                raise
            if path.startswith("/api/"):
                await reject(500, "An unexpected server error occurred.")
            else:
                raise
        finally:
            _write_request_log(request_id, method, _route_name(scope), status_code, (time.perf_counter() - started) * 1000)
