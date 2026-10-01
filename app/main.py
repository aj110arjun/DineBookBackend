from fastapi import FastAPI
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
from app.routers.customer_auth import session_router


app = FastAPI(title="DineBook API", version="0.1.0")

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
    allow_headers=["Content-Type", "Authorization"],
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


@app.get("/api/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
