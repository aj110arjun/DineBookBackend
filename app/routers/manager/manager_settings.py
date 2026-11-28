from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.manager_auth import current_manager
from app.schemas.customer_settings import GeneralSettingsUpdate
from app.schemas.manager_settings import ManagerNotificationSettings, ManagerSettingsResponse
from app.services.manager_settings import manager_settings_response

router = APIRouter(prefix="/api/manager/settings", tags=["manager settings"])


@router.get("", response_model=ManagerSettingsResponse)
def get_settings(manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return manager_settings_response(db, manager)


@router.patch("/notifications", response_model=ManagerSettingsResponse)
def patch_notifications(payload: ManagerNotificationSettings, manager: User = Depends(current_manager),
                        db: Session = Depends(get_db)):
    return manager_settings_response(db, manager, section="notifications", changes=payload.model_dump(exclude_unset=True))


@router.patch("/general", response_model=ManagerSettingsResponse)
def patch_general(payload: GeneralSettingsUpdate, manager: User = Depends(current_manager), db: Session = Depends(get_db)):
    return manager_settings_response(db, manager, section="general", changes=payload.model_dump(exclude_unset=True))
