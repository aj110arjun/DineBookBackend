from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.chef_auth import current_chef
from app.schemas.chef_settings import ChefNotificationSettings, ChefSettingsResponse
from app.schemas.customer_settings import GeneralSettingsUpdate
from app.services.chef_settings import chef_settings_response

router = APIRouter(prefix="/api/chef/settings", tags=["chef settings"])


@router.get("", response_model=ChefSettingsResponse)
def get_settings(chef: User = Depends(current_chef), db: Session = Depends(get_db)):
    return chef_settings_response(db, chef)


@router.patch("/notifications", response_model=ChefSettingsResponse)
def patch_notifications(payload: ChefNotificationSettings, chef: User = Depends(current_chef),
                        db: Session = Depends(get_db)):
    return chef_settings_response(db, chef, section="notifications", changes=payload.model_dump(exclude_unset=True))


@router.patch("/general", response_model=ChefSettingsResponse)
def patch_general(payload: GeneralSettingsUpdate, chef: User = Depends(current_chef), db: Session = Depends(get_db)):
    return chef_settings_response(db, chef, section="general", changes=payload.model_dump(exclude_unset=True))
