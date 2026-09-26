from pydantic import BaseModel, EmailStr, Field


class ManagerRegisterRequest(BaseModel):
    # Manager details
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    # Restaurant details
    restaurant_name: str = Field(min_length=2, max_length=255)
    restaurant_description: str | None = None
    restaurant_phone: str | None = Field(default=None, max_length=20)
    restaurant_address: str = Field(min_length=2)
    restaurant_city: str = Field(min_length=2, max_length=100)
    latitude: float | None = None
    longitude: float | None = None