from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict

from app.models.restaurant import RestaurantStatus
from app.schemas.customer import CustomerProfileUpdate, CustomerResponse


class ChefProfileUpdate(CustomerProfileUpdate):
    """Only the chef's name and phone may be changed through the profile."""


class ChefDetails(CustomerResponse):
    # Existing account addresses, including legacy ones, remain displayable.
    email: str
    updated_at: datetime


class ChefRestaurantDetails(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    cuisine_type: str | None
    phone: str | None
    address: str
    city: str
    state: str | None
    pin_code: str | None
    status: RestaurantStatus


class ChefProfileResponse(BaseModel):
    chef: ChefDetails
    restaurant: ChefRestaurantDetails | None
