"""Manager preferences reuse the existing account and notification storage."""
from pydantic import BaseModel, ConfigDict, StrictBool

from app.schemas.customer_settings import GeneralSettings


class ManagerNotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_app_enabled: StrictBool = True
    reservation_confirmation: StrictBool = True
    reservation_reminders: StrictBool = True
    reservation_updates: StrictBool = True
    order_confirmation: StrictBool = True
    order_status_updates: StrictBool = True
    order_completion: StrictBool = True
    payment_confirmation: StrictBool = True
    refund_updates: StrictBool = True
    wallet_transactions: StrictBool = True
    review_updates: StrictBool = True
    staff_updates: StrictBool = True
    approval_updates: StrictBool = True


class ManagerSettingsResponse(BaseModel):
    notifications: ManagerNotificationSettings
    general: GeneralSettings
