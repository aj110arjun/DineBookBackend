import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.user import AccountStatus, UserRole
from app.schemas.customer import CustomerProfileUpdate


class AdminProfileUpdate(CustomerProfileUpdate):
	"""Allow only validated name and phone updates."""


class AdminDetails(BaseModel):
	model_config = ConfigDict(from_attributes=True)

	id: uuid.UUID
	name: str
	email: str
	phone: str | None = None
	avatar_url: str | None = None
	role: UserRole
	status: AccountStatus
	is_active: bool
	created_at: datetime
	updated_at: datetime


class AdminProfileResponse(BaseModel):
	admin: AdminDetails