from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
import uuid
from datetime import datetime

from app.models.user import AccountStatus, UserRole
from app.core.security import validate_new_password
from app.core.phone import get_restaurant_phone_error


class CustomerRegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    _password_policy = field_validator("password")(validate_new_password)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = " ".join(value.split())
        if len(name) < 2:
            raise ValueError("Name must contain at least 2 characters")
        if any(not (character.isalpha() or character.isspace() or character in "'-.") for character in name):
            raise ValueError("Name may only contain letters, spaces, apostrophes, hyphens, and periods")
        return name

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @model_validator(mode="after")
    def passwords_must_match(self):
        if self.password != self.confirm_password:
            raise ValueError("Passwords do not match")
        return self


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    email: EmailStr
    role: UserRole
    status: AccountStatus
    is_active: bool
    can_change_email: bool = False
    can_change_password: bool = False
    phone: str | None = None
    avatar_url: str | None = None
    created_at: datetime


class CustomerProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("name", mode="before")
    @classmethod
    def clean_name(cls, value):
        if not isinstance(value, str):
            raise ValueError("Full name is required.")
        return CustomerRegisterRequest.normalize_name(value)

    @field_validator("phone", mode="before")
    @classmethod
    def clean_phone(cls, value):
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Enter a valid phone number.")
        phone = value.strip()
        if not phone:
            return None
        error = get_restaurant_phone_error(phone)
        if error:
            raise ValueError(error)
        return phone
