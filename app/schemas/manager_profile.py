from datetime import date, datetime
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.phone import get_restaurant_phone_error
from app.models.restaurant import RestaurantStatus
from app.models.restaurant_document import RestaurantDocumentType
from app.schemas.customer import CustomerProfileUpdate, CustomerResponse
from app.schemas.manager import ManagerRegisterRequest


class ManagerProfileUpdate(CustomerProfileUpdate):
    """Only name and phone; the shared schema forbids account-control fields."""


class ManagerDetails(CustomerResponse):
    # Read the stored account address as-is; only editable contact emails need
    # fresh validation. Legacy/dev accounts must not fail response serialization.
    email: str
    updated_at: datetime


class RestaurantProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    cuisine_type: str | None = Field(default=None, min_length=2, max_length=100)
    email: EmailStr | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, min_length=7, max_length=20)
    address: str | None = Field(default=None, min_length=2, max_length=500)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    state: str | None = Field(default=None, min_length=2, max_length=100)
    pin_code: str | None = Field(default=None, min_length=3, max_length=10)

    @field_validator("name", "cuisine_type", "phone", "address", "city", "state", "pin_code", mode="before")
    @classmethod
    def required_text(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("This field cannot be blank or null.")
        return " ".join(value.split())

    @field_validator("name")
    @classmethod
    def name_characters(cls, value):
        return ManagerRegisterRequest.reject_name_symbols(value)

    @field_validator("description", mode="before")
    @classmethod
    def description_text(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Enter a description as text.")
        return value.strip() or None

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value):
        error = get_restaurant_phone_error(value)
        if error:
            raise ValueError(error)
        return value

    @field_validator("pin_code")
    @classmethod
    def valid_postcode(cls, value):
        import re
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 -]{1,8}[A-Za-z0-9]", value):
            raise ValueError("Enter a valid PIN or ZIP code (3–10 characters).")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def contact_email(cls, value):
        return value.strip().lower() or None if isinstance(value, str) else value


class ProfileDocument(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    document_type: RestaurantDocumentType
    file_name: str
    created_at: datetime


class OperatingPeriod(BaseModel):
    opens: str
    closes: str


class RestaurantOperation(BaseModel):
    date: date
    timezone: str
    is_open_now: bool | None
    schedule_configured: bool
    periods: list[OperatingPeriod]


class RestaurantDetails(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str | None
    cuisine_type: str | None
    email: str | None
    phone: str | None
    address: str
    city: str
    state: str | None
    pin_code: str | None
    latitude: float | None
    longitude: float | None
    status: RestaurantStatus
    created_at: datetime
    updated_at: datetime
    branding_image_url: str | None
    documents: list[ProfileDocument]
    operation: RestaurantOperation


class ManagerProfileResponse(BaseModel):
    manager: ManagerDetails
    restaurant: RestaurantDetails | None
