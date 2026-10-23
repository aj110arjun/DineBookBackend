from app.routers.auth.customer_auth import router as customer_auth_router
from app.routers.auth.admin_auth import router as admin_auth_router
from app.routers.auth.manager_auth import router as manager_auth_router
from app.routers.admin.admin_managers import router as admin_managers_router
from app.routers.auth.chef_auth import router as chef_auth_router, session_router as chef_session_router
from app.routers.manager.manager_staff import router as manager_staff_router
from app.routers.manager.manager_menu import router as manager_menu_router
from app.routers.customer.customer_restaurants import router as customer_restaurants_router
from app.routers.admin.admin_restaurants import router as admin_restaurants_router
from app.routers.auth.password_recovery import router as password_recovery_router


__all__ = ["customer_auth_router", "customer_restaurants_router", "admin_restaurants_router", "admin_auth_router", "manager_auth_router", "manager_staff_router", "manager_menu_router", "admin_managers_router", "chef_auth_router", "chef_session_router", "password_recovery_router"]
