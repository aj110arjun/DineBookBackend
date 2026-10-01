import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.orm import Session

from app.core.email import send_branded_email
from app.core.otp import verification_code_expired
from app.core.security import hash_password, verify_secret
from app.db.database import get_db
from app.models.user import EmailVerificationCode, User, UserRole


router = APIRouter(prefix="/api/auth", tags=["password recovery"])
PortalRole = Literal["admin", "manager", "chef"]
ROLE_MAP = {
    "admin": UserRole.ADMIN,
    "manager": UserRole.MANAGER,
    "chef": UserRole.CHEF,
}


class RecoveryEmailRequest(BaseModel):
    email: EmailStr


class RecoveryResetRequest(RecoveryEmailRequest):
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not any(char.isalpha() for char in value):
            raise ValueError("Password must include a letter.")
        if not any(not char.isalpha() and not char.isspace() for char in value):
            raise ValueError("Password must include a number or symbol.")
        return value


def send_portal_reset_email(email: str, code: str, role: str) -> None:
    role_name = role.capitalize()
    send_branded_email(
        to=email,
        subject=f"Your DineBook {role_name.lower()} password reset code",
        title="Reset your password",
        intro=f"We received a request to reset the password for your DineBook {role_name.lower()} account.",
        detail="This reset code expires in 2 minutes. If you didn’t request a password reset, ignore this email and your password will remain unchanged.",
        highlight_label="Your reset code",
        highlight_value=code,
        plain_text=f"Your DineBook {role_name.lower()} password reset code is {code}. It expires in 2 minutes.",
        error_detail="We couldn’t send the reset email. Please try again.",
    )


@router.post("/{portal_role}/forgot-password")
def request_portal_password_reset(
    portal_role: PortalRole,
    payload: RecoveryEmailRequest,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    role = ROLE_MAP[portal_role]
    email = str(payload.email).strip().lower()
    account = db.query(User).filter(User.email == email, User.role == role).first()
    response = {"message": "If an account exists, a password reset code has been sent."}
    if account is None:
        return response

    code = f"{secrets.randbelow(1_000_000):06d}"
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    if verification is None:
        verification = EmailVerificationCode(email=email, code_hash=hash_password(code), expires_at=expires_at)
        db.add(verification)
    else:
        verification.code_hash = hash_password(code)
        verification.expires_at = expires_at
    try:
        db.flush()
        send_portal_reset_email(email, code, portal_role)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return response


@router.post("/{portal_role}/reset-password")
def reset_portal_password(
    portal_role: PortalRole,
    payload: RecoveryResetRequest,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    role = ROLE_MAP[portal_role]
    email = str(payload.email).strip().lower()
    account = db.query(User).filter(User.email == email, User.role == role).first()
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    now = datetime.now(timezone.utc)
    if account is None or verification is None or verification_code_expired(verification.expires_at, now):
        raise HTTPException(status_code=400, detail="That reset code has expired. Request a new code.")
    if not verify_secret(payload.code, verification.code_hash):
        raise HTTPException(status_code=400, detail="That reset code is incorrect.")
    account.password_hash = hash_password(payload.new_password)
    db.delete(verification)
    db.commit()
    return {"message": "Your password has been reset. You can now sign in."}
