from app.routers.customer_auth import router as customer_auth_router
from app.routers.admin_auth import router as admin_auth_router
from app.routers.manager_auth import router as manager_auth_router
from app.routers.admin_managers import router as admin_managers_router
from app.routers.chef_auth import router as chef_auth_router, session_router as chef_session_router


__all__ = ["customer_auth_router", "admin_auth_router", "manager_auth_router", "admin_managers_router", "chef_auth_router", "chef_session_router"]
