from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.admin_auth import current_admin
from app.schemas.admin_dashboard import (
    ActivityPage, ActivityQuery, AdminDashboardResponse, AdminPeriodQuery,
    CustomerAnalytics, OrderAnalytics, PaymentAnalytics, ReservationAnalytics,
    RestaurantPage, RestaurantQuery, RevenueAnalytics,
)
from app.services import admin_dashboard as analytics

router = APIRouter(prefix="/api/admin/dashboard", tags=["admin dashboard"])
Admin = Annotated[User, Depends(current_admin)]
Database = Annotated[Session, Depends(get_db)]
Period = Annotated[AdminPeriodQuery, Query()]


@router.get("/overview", response_model=AdminDashboardResponse)
def overview(params: Period, admin: Admin, db: Database):
    return analytics.dashboard(db, admin, params)


@router.get("/revenue", response_model=RevenueAnalytics)
def revenue(params: Period, admin: Admin, db: Database):
    return analytics.revenue(db, params)


@router.get("/restaurants", response_model=RestaurantPage)
def restaurants(params: Annotated[RestaurantQuery, Query()], admin: Admin, db: Database):
    return analytics.restaurant_performance(db, params)


@router.get("/orders", response_model=OrderAnalytics)
def orders(params: Period, admin: Admin, db: Database):
    return analytics.operations(db, params)


@router.get("/reservations", response_model=ReservationAnalytics)
def reservations(params: Period, admin: Admin, db: Database):
    return analytics.operations(db, params, reservations=True)


@router.get("/customers", response_model=CustomerAnalytics)
def customers(params: Period, admin: Admin, db: Database):
    return analytics.customers(db, params)


@router.get("/payments", response_model=PaymentAnalytics)
def payments(params: Period, admin: Admin, db: Database):
    return analytics.payments(db, params)


@router.get("/activity", response_model=ActivityPage)
def activity(params: Annotated[ActivityQuery, Query()], admin: Admin, db: Database):
    return analytics.activity(db, admin, params)
