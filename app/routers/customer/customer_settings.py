from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.models.user import User
from app.routers.auth.customer_auth import current_customer
from app.schemas.customer_settings import CustomerSettingsResponse, GeneralSettingsUpdate, NotificationSettingsUpdate
from app.services.customer_settings import settings_response

router = APIRouter(prefix="/api/customer/settings", tags=["customer settings"])


@router.get("", response_model=CustomerSettingsResponse)
def get_settings(customer: User = Depends(current_customer), db: Session = Depends(get_db)):
    return settings_response(db, customer)


@router.patch("/notifications", response_model=CustomerSettingsResponse)
def patch_notifications(payload: NotificationSettingsUpdate, customer: User = Depends(current_customer), db: Session = Depends(get_db)):
    return settings_response(db, customer, section="notifications", changes=payload.model_dump(exclude_unset=True))


@router.patch("/general", response_model=CustomerSettingsResponse)
def patch_general(payload: GeneralSettingsUpdate, customer: User = Depends(current_customer), db: Session = Depends(get_db)):
    return settings_response(db, customer, section="general", changes=payload.model_dump(exclude_unset=True))
