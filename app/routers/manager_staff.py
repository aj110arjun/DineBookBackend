import uuid
import secrets
import smtplib
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole
from app.routers.manager_auth import current_manager

router = APIRouter(prefix="/api/manager/staff", tags=["manager staff"])


class ChefCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 2:
            raise ValueError("Name must contain at least 2 characters.")
        return value


def chef_dict(chef: User) -> dict:
    return {"id": str(chef.id), "name": chef.name, "email": chef.email, "role": chef.role.value, "status": chef.status.value, "is_active": chef.is_active}


def send_chef_credentials(email: str, name: str, password: str) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise HTTPException(status_code=503, detail="Email delivery is not configured. Please contact support.")
    message = EmailMessage()
    message["Subject"] = "Your DineBook chef account"
    message["From"] = settings.smtp_from_email
    message["To"] = email
    message.set_content(
        f"Hello {name},\n\nYour manager created a DineBook chef account for you.\n"
        f"Sign in at {settings.frontend_public_url or settings.frontend_url}/chef/login\n\n"
        f"Email: {email}\nTemporary password: {password}\n\n"
        "You will be asked to change this password when you first sign in."
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(status_code=503, detail="We couldn’t send the chef’s account email. Please try again.") from exc



@router.get("")
def list_staff(manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> list[dict]:
    staff = db.query(User).filter(User.manager_id == manager.id, User.role == UserRole.CHEF).order_by(User.created_at.desc()).all()
    return [chef_dict(chef) for chef in staff]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_chef(payload: ChefCreateRequest, manager: User = Depends(current_manager), db: Session = Depends(get_db)) -> dict:
    email = str(payload.email).strip().lower()
    if db.query(User.id).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    temporary_password = secrets.token_urlsafe(12)
    send_chef_credentials(email, payload.name, temporary_password)
    chef = User(
        name=payload.name,
        email=email,
        password_hash=hash_password(temporary_password),
        role=UserRole.CHEF,
        status=AccountStatus.ACTIVE,
        is_active=True,
        email_verified=True,
        must_change_password=True,

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
