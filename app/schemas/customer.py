import unicodedata
import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.user import AccountStatus, UserRole


COMMON_PASSWORDS = {
    "12345678", "123456789", "1234567890", "password", "password1",
    "password123", "qwerty123", "qwertyuiop", "letmein123", "welcome1",
    "admin123", "iloveyou1", "abc12345", "p@ssw0rd", "passw0rd",
    "password1!", "qwerty123!", "welcome123!", "p@ssword1", "admin123!",
}


class CustomerRegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        name = unicodedata.normalize("NFC", " ".join(value.split()))
        if len(name) < 2 or any(not (char.isalpha() or char.isspace()) for char in name):
            raise ValueError("Full name can contain only letters and spaces")
        if not any(char.isalpha() for char in name):
            raise ValueError("Full name must include letters")
        return name

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        if not any(char.isupper() for char in value):
            raise ValueError("Password must include an uppercase letter")
        if not any(char.islower() for char in value):
            raise ValueError("Password must include a lowercase letter")
        if not any(unicodedata.category(char) == "Nd" for char in value):
            raise ValueError("Password must include a number")
        if not any(not char.isalnum() for char in value):
            raise ValueError("Password must include a special symbol")
        if value.casefold() in COMMON_PASSWORDS:
            raise ValueError("Choose a less common password")
        return value

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
