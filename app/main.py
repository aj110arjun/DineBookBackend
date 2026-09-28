from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.routers import (
    admin_auth_router,
    admin_managers_router,
    chef_auth_router,
    chef_session_router,
    customer_auth_router,
    manager_auth_router,
)


app = FastAPI(title="DineBook API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
)

app.include_router(customer_auth_router)
app.include_router(session_router)
app.include_router(admin_auth_router)
app.include_router(manager_auth_router)
app.include_router(chef_auth_router)
app.include_router(chef_session_router)
app.include_router(admin_managers_router)


@app.get("/api/health", tags=["health"])
def health_check() -> dict[str, str]:
    return {"status": "ok"}
