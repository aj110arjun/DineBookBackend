from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.manager_auth import current_manager
from app.routers.restaurants.dining import get_manager_restaurant
from app.schemas.manager_dashboard import DashboardQuery, DashboardResponse
from app.services.manager_dashboard import dashboard

router = APIRouter(prefix="/api/manager/dashboard", tags=["manager dashboard"])


@router.get("/overview", response_model=DashboardResponse)
def manager_dashboard(
    params: Annotated[DashboardQuery, Query()],
    manager: User = Depends(current_manager),
    db: Session = Depends(get_db),
) -> DashboardResponse:
    return dashboard(db, get_manager_restaurant(manager, db), manager, params)
