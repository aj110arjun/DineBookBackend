from pydantic import BaseModel, ConfigDict, StrictBool

from app.schemas.customer_settings import GeneralSettings


class AdminNotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_app_enabled: StrictBool = True
    approval_updates: StrictBool = True
    review_updates: StrictBool = True
    wallet_transactions: StrictBool = True
    system_updates: StrictBool = True


class AdminSettingsResponse(BaseModel):
    notifications: AdminNotificationSettings
    general: GeneralSettings
