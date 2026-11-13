from typing import Literal
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

IANA_TIMEZONES = available_timezones() - {"localtime", "Factory"}


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_enabled: StrictBool = True
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
    promotional_offers: StrictBool = False
    review_updates: StrictBool = True


class NotificationSettingsUpdate(NotificationSettings):
    # Defaults are omitted by exclude_unset; explicitly supplied nulls are invalid.
    pass


class GeneralSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    theme: Literal["light", "dark", "system"] = "system"
    language: Literal["en"] = "en"
    date_format: Literal["DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD"] = "DD/MM/YYYY"
    time_format: Literal["12h", "24h"] = "12h"
    timezone: str | None = Field(default=None, max_length=100)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        if value is not None and (value not in IANA_TIMEZONES or value.startswith(("posix/", "right/"))):
            raise ValueError("Choose a valid IANA timezone.")
        return value


class GeneralSettingsUpdate(GeneralSettings):
    pass


class CustomerSettingsResponse(BaseModel):
    notifications: NotificationSettings
    general: GeneralSettings
