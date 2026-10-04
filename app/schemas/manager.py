from pydantic import BaseModel, EmailStr, Field, field_validator


class ManagerRegisterRequest(BaseModel):
    # Manager details
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    # Restaurant details
    restaurant_name: str = Field(min_length=2, max_length=255)
    restaurant_description: str | None = Field(default=None, max_length=2000)
    cuisine_type: str = Field(min_length=2, max_length=100)

    restaurant_contact: str = Field(min_length=6, max_length=20)
    restaurant_email: EmailStr

    # Address
    address: str = Field(min_length=2, max_length=500)
    city: str = Field(min_length=2, max_length=100)
    state: str = Field(min_length=2, max_length=100)
    pin_code: str = Field(min_length=3, max_length=20)

    # Restaurant capacity
    capacity: int = Field(gt=0)
    tables: int = Field(gt=0)

    @field_validator("name", "restaurant_name", "cuisine_type", "restaurant_contact", "address", "city", "state", "pin_code")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("This field is required")
        return value

    @field_validator("name", "restaurant_name")
    @classmethod
    def reject_name_symbols(cls, value: str) -> str:
        if any(not (character.isalnum() or character.isspace() or character in "'-.") for character in value):
            raise ValueError("Name may only contain letters, numbers, spaces, apostrophes, hyphens, and periods")
        return value

    @field_validator("email", "restaurant_email")
    @classmethod
    def normalize_emails(cls, value: EmailStr) -> str:
        return str(value).strip().lower()
