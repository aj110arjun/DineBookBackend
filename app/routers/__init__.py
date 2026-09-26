from app.routers.customer_auth import router as customer_auth_router
from app.routers.admin_auth import router as admin_auth_router

__all__ = ["customer_auth_router", "admin_auth_router"]
