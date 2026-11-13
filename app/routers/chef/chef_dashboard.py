from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.chef_auth import current_chef
from app.routers.commerce.orders import staff_restaurant
from app.schemas.chef_dashboard import ChefDashboardQuery, ChefDashboardResponse
from app.services.chef_dashboard import dashboard

router = APIRouter(prefix="/api/chef/dashboard", tags=["chef dashboard"])


@router.get("", response_model=ChefDashboardResponse)
def chef_dashboard(
    params: Annotated[ChefDashboardQuery, Query()],
    chef: User = Depends(current_chef),
    db: Session = Depends(get_db),
) -> ChefDashboardResponse:
    return dashboard(db, staff_restaurant(chef, db), params)
