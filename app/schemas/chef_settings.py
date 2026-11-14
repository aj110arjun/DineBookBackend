"""Chef preferences use the shared general settings and account storage."""
from pydantic import BaseModel, ConfigDict, StrictBool

from app.schemas.customer_settings import GeneralSettings


class ChefNotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_app_enabled: StrictBool = True
    order_confirmation: StrictBool = True
    order_status_updates: StrictBool = True
    order_completion: StrictBool = True
    payment_confirmation: StrictBool = True
    refund_updates: StrictBool = True
    wallet_transactions: StrictBool = True


class ChefSettingsResponse(BaseModel):
    notifications: ChefNotificationSettings
    general: GeneralSettings
