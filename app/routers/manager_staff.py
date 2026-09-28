import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole
from app.routers.manager_auth import current_manager

router = APIRouter(prefix="/api/manager/staff", tags=["manager staff"])


class ChefCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Name must contain at least 2 characters.")
        return value


def chef_dict(chef: User) -> dict:
  
    return {"id": str(chef.id), "name": chef.name, "email": chef.email, "role": chef.role.value, "status": chef.status.value, "is_active": chef.is_active}



@router.get("")
def list_staff(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    staff = db.query(User).filter(User.manager_id == manager.id, User.role == UserRole.CHEF).order_by(User.created_at.desc()).all()
    return [chef_dict(chef) for chef in staff]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_chef(payload: ChefCreateRequest, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    email = str(payload.email).strip().lower()
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    chef = User(
        name=payload.name,
        email=email,
        password_hash=hash_password(payload.password),
        role=UserRole.CHEF,
        status=AccountStatus.ACTIVE,
        is_active=True,
        email_verified=True,

        manager_id=manager.id,
    )
    db.add(chef)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="An account with this email already exists.") from exc
    db.refresh(chef)
    return chef_dict(chef)


@router.delete("/{chef_id}")
def deactivate_chef(chef_id: uuid.UUID, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict[str, str]:
    chef = db.query(User).filter(User.id == chef_id, User.manager_id == manager.id, User.role == UserRole.CHEF).first()
    if chef is None:
        raise HTTPException(status_code=404, detail="Chef staff member not found.")
    chef.is_active = False
    chef.status = AccountStatus.INACTIVE
    db.commit()
    return {"message": "Chef account deactivated."}
