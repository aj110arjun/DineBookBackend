from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
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
from app.routers.reservations import customer_router as customer_reservations_router, manager_router as manager_reservations_router, chef_router as chef_reservations_router
from app.routers.customer_auth import session_router
from app.routers.wallet import customer_router as customer_wallet_router, admin_router as admin_wallet_router
from app.routers.orders import customer_router as customer_orders_router, customer_reservation_orders_router, manager_router as manager_orders_router, manager_reservation_orders_router, chef_router as chef_orders_router, chef_reservation_orders_router, admin_router as admin_orders_router
from app.routers.webhooks import router as webhooks_router
from app.routers.realtime import router as realtime_router


app = FastAPI(title="DineBook API", version="0.1.0")

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    method = request.method.upper()
    path = request.url.path

    if method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site", "").lower()
        trusted_origins = {settings.frontend_url.rstrip("/")}
        if settings.frontend_public_url:
            trusted_origins.add(settings.frontend_public_url.rstrip("/"))
        if site == "cross-site" or (origin and origin.rstrip("/") not in trusted_origins):
            return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)
    elif path.startswith("/api/events/"):
        origin = request.headers.get("origin")
        trusted_origins = {settings.frontend_url.rstrip("/")}
        if settings.frontend_public_url:
            trusted_origins.add(settings.frontend_public_url.rstrip("/"))
        if origin and origin.rstrip("/") not in trusted_origins:
            return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    # The backend serves a small inline Google OAuth popup page; all other
    # resources remain blocked on that response. Frontend hosting must set its
    # own CSP for the actual application document.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    )
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
    settings.frontend_url,
    *(
        [settings.frontend_public_url]
        if settings.frontend_public_url
        else []
    ),
],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "Last-Event-ID"],
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
app.include_router(admin_wallet_router)
app.include_router(manager_reservations_router)
app.include_router(chef_reservations_router)
app.include_router(customer_orders_router)
app.include_router(customer_reservation_orders_router)
app.include_router(manager_orders_router)
app.include_router(manager_reservation_orders_router)
app.include_router(chef_orders_router)
app.include_router(chef_reservation_orders_router)
app.include_router(admin_orders_router)
app.include_router(webhooks_router)
app.include_router(realtime_router)


@app.get("/api/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
