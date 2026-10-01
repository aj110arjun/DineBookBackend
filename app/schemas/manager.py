from pydantic import BaseModel, EmailStr, Field


class ManagerRegisterRequest(BaseModel):
    # Manager details
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    # Restaurant details
    restaurant_name: str = Field(min_length=2, max_length=255)
    restaurant_description: str | None = None
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
