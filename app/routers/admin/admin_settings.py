from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.admin_auth import current_admin
from app.schemas.admin_settings import (
    AdminNotificationSettings,
    AdminSettingsResponse,
)
from app.schemas.customer_settings import GeneralSettingsUpdate
from app.services.admin_settings import admin_settings_response


router = APIRouter(
    prefix="/api/admin/settings",
    tags=["admin settings"],
)


@router.get("", response_model=AdminSettingsResponse)
def get_settings(
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    return admin_settings_response(db, admin)


@router.patch("/notifications", response_model=AdminSettingsResponse)
def patch_notifications(
    payload: AdminNotificationSettings,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    return admin_settings_response(
        db,
        admin,
        section="notifications",
        changes=payload.model_dump(exclude_unset=True),
    )


@router.patch("/general", response_model=AdminSettingsResponse)
def patch_general(
    payload: GeneralSettingsUpdate,
    admin: User = Depends(current_admin),
    db: Session = Depends(get_db),
):
    return admin_settings_response(
        db,
        admin,
        section="general",
        changes=payload.model_dump(exclude_unset=True),
    )