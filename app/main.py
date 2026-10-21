import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.core.config import settings
from app.middleware import RequestGuardMiddleware
from app.routers import (
    admin_auth_router,
    admin_managers_router,
    admin_restaurants_router,
    chef_auth_router,
    chef_session_router,
    customer_auth_router,
    customer_restaurants_router,
    manager_auth_router,
    manager_staff_router,
    manager_menu_router,
    password_recovery_router,
)
from app.routers.dining import admin_router as admin_dining_router, chef_router as chef_dining_router, customer_router as customer_dining_router, manager_router as manager_dining_router
from app.routers.reservations import customer_router as customer_reservations_router, manager_router as manager_reservations_router, manager_calendar_router, chef_router as chef_reservations_router
from app.routers.customer_auth import session_router
from app.routers.wallet import customer_router as customer_wallet_router, admin_router as admin_wallet_router, manager_router as manager_wallet_router, chef_wallet_router
from app.routers.orders import customer_router as customer_orders_router, customer_reservation_orders_router, manager_router as manager_orders_router, manager_reservation_orders_router, chef_router as chef_orders_router, chef_reservation_orders_router, admin_router as admin_orders_router
from app.routers.webhooks import router as webhooks_router
from app.routers.payments import manager_router as manager_payments_router, admin_router as admin_payments_router
from app.routers.realtime import router as realtime_router
from app.routers.notifications import router as notifications_router
from app.routers.operating_hours import admin_router as admin_operating_hours_router, chef_router as chef_operating_hours_router, customer_router as customer_operating_hours_router, manager_router as manager_operating_hours_router
from app.routers.customer_transactions import router as customer_transactions_router
from app.routers.reviews import customer_router as customer_reviews_router, public_router as public_reviews_router, chef_router as chef_reviews_router, manager_router as manager_reviews_router, admin_router as admin_reviews_router
from app.routers.revenue import admin_router as admin_revenue_router, manager_router as manager_revenue_router, chef_router as chef_revenue_router


app = FastAPI(title="DineBook API", version="0.1.0")


def apply_security_headers(response, request_id: str, duration_ms: float) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    )
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    started_at = time.perf_counter()
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())
    request.state.request_id = request_id
    method = request.method.upper()
    path = request.url.path

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            request_size = int(content_length)
        except ValueError:
            response = JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)
            apply_security_headers(response, request_id, (time.perf_counter() - started_at) * 1000)
            return response
        if request_size < 0:
            response = JSONResponse({"detail": "Invalid Content-Length header."}, status_code=400)
            apply_security_headers(response, request_id, (time.perf_counter() - started_at) * 1000)
            return response
        if request_size > settings.max_request_body_bytes:
            response = JSONResponse({"detail": "Request body is too large."}, status_code=413)
            apply_security_headers(response, request_id, (time.perf_counter() - started_at) * 1000)
            return response

    if method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site", "").lower()
        trusted_origins = {settings.frontend_url.rstrip("/")}
        if settings.frontend_public_url:
            trusted_origins.add(settings.frontend_public_url.rstrip("/"))
        if site == "cross-site" or (origin and origin.rstrip("/") not in trusted_origins):
            response = JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
            apply_security_headers(response, request_id, (time.perf_counter() - started_at) * 1000)
            return response
    elif path.startswith("/api/events/"):
        origin = request.headers.get("origin")
        trusted_origins = {settings.frontend_url.rstrip("/")}
        if settings.frontend_public_url:
            trusted_origins.add(settings.frontend_public_url.rstrip("/"))
        if origin and origin.rstrip("/") not in trusted_origins:
            response = JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
            apply_security_headers(response, request_id, (time.perf_counter() - started_at) * 1000)
            return response

    response = await call_next(request)
    apply_security_headers(response, request_id, (time.perf_counter() - started_at) * 1000)
    if path in {app.docs_url, app.swagger_ui_oauth2_redirect_url}:
        # FastAPI's Swagger HTML loads its bundle/styles from jsDelivr and
        # fetches the same-origin OpenAPI schema. Keep the API policy strict.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "connect-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
            "font-src 'self' data:; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
        )
    if path.startswith("/api/"):
        # Public restaurant discovery data is requested repeatedly during
        # navigation. Allow a short private browser cache while keeping all
        # account, order, payment, and manager/admin responses uncached.
        is_public_restaurant_read = (
            method in {"GET", "HEAD"}
            and (path == "/api/customer/restaurants" or path.startswith("/api/customer/restaurants/"))
        )
        if is_public_restaurant_read:
            response.headers.setdefault("Cache-Control", "private, max-age=15")
        else:
            response.headers.setdefault("Cache-Control", "no-store")
    return response

app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
app.add_middleware(RequestGuardMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        settings.frontend_url,
        *([settings.frontend_public_url] if settings.frontend_public_url else []),
    ],
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "OPTIONS", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "Last-Event-ID"],
    expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-Request-ID"],
)

app.include_router(customer_auth_router)
app.include_router(password_recovery_router)
app.include_router(customer_restaurants_router)
app.include_router(session_router)
app.include_router(admin_auth_router)
app.include_router(manager_auth_router)
app.include_router(manager_staff_router)
app.include_router(manager_menu_router)
app.include_router(chef_auth_router)
app.include_router(chef_session_router)
app.include_router(admin_managers_router)
app.include_router(admin_restaurants_router)
app.include_router(manager_dining_router)
app.include_router(chef_dining_router)
app.include_router(customer_dining_router)
app.include_router(admin_dining_router)
app.include_router(customer_reservations_router)
app.include_router(customer_wallet_router)
app.include_router(customer_transactions_router)
app.include_router(customer_reviews_router)
app.include_router(public_reviews_router)
app.include_router(chef_reviews_router)
app.include_router(manager_reviews_router)
app.include_router(admin_reviews_router)
app.include_router(admin_wallet_router)
app.include_router(manager_wallet_router)
app.include_router(chef_wallet_router)
app.include_router(admin_revenue_router)
app.include_router(manager_revenue_router)
app.include_router(chef_revenue_router)
app.include_router(manager_reservations_router)
app.include_router(manager_calendar_router)
app.include_router(chef_reservations_router)
app.include_router(customer_orders_router)
app.include_router(customer_reservation_orders_router)
app.include_router(manager_orders_router)
app.include_router(manager_reservation_orders_router)
app.include_router(chef_orders_router)
app.include_router(chef_reservation_orders_router)
app.include_router(admin_orders_router)
app.include_router(webhooks_router)
app.include_router(manager_payments_router)
app.include_router(admin_payments_router)
app.include_router(realtime_router)
app.include_router(notifications_router)
app.include_router(customer_operating_hours_router)
app.include_router(manager_operating_hours_router)
app.include_router(chef_operating_hours_router)
app.include_router(admin_operating_hours_router)


@app.get("/api/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
