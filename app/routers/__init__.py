from app.routers.customer_auth import router as customer_auth_router
from app.routers.admin_auth import router as admin_auth_router
from app.routers.manager_auth import router as manager_auth_router
from app.routers.admin_managers import router as admin_managers_router


__all__ = ["customer_auth_router", "admin_auth_router", "manager_auth_router", "admin_managers_router"]
