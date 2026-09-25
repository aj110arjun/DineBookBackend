import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password, verify_secret
from app.db.database import get_db
from app.models.user import AccountStatus, EmailVerificationCode, User, UserRole
from app.schemas.customer import CustomerRegisterRequest, CustomerResponse

router = APIRouter(prefix="/api/auth/customer", tags=["customer authentication"])


class VerificationRequest(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class ResendVerificationRequest(BaseModel):
    email: EmailStr


def issue_verification_code(db: Session, email: str) -> None:
    if not (settings.smtp_host and settings.smtp_from_email):
        raise HTTPException(status_code=503, detail="Email delivery is not configured. Set SMTP_HOST and SMTP_FROM_EMAIL in the backend environment.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    if challenge is None:
        challenge = EmailVerificationCode(email=email, code_hash=hash_password(code), expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
        db.add(challenge)
    else:
        challenge.code_hash = hash_password(code)
        challenge.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        challenge.created_at = datetime.now(timezone.utc)
    message = EmailMessage()
    message["Subject"] = "Your DineBook email confirmation code"
    message["From"] = settings.smtp_from_email
    message["To"] = email
    message.set_content(f"Your DineBook confirmation code is {code}. It expires in 10 minutes. If you did not create an account, you can ignore this email.")
    try:
        if settings.smtp_use_tls:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                server.starttls()
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password or "")
                server.send_message(message)
        else:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15) as server:
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password or "")
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="We couldn’t send your confirmation email. Please try again shortly.") from exc


@router.post("/register", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
def register_customer(payload: CustomerRegisterRequest, db: Session = Depends(get_db)) -> User:
    email = str(payload.email).strip().lower()
    try:
        existing_user = db.query(User.id).filter(User.email == email).first()
    except ProgrammingError as exc:
        db.rollback()
        # PostgreSQL SQLSTATE 42P01 means the users table is missing. Keep the
        # response actionable without exposing database connection details.
        if getattr(exc.orig, "sqlstate", None) == "42P01" or getattr(exc.orig, "pgcode", None) == "42P01":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Customer database schema is not initialized. From Backend/, run 'venv/bin/alembic upgrade head'.",
            ) from exc
        raise
    if existing_user:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.")

    customer = User(
        name=payload.name,
        email=email,
        password_hash=hash_password(payload.password),
        role=UserRole.CUSTOMER,
        status=AccountStatus.ACTIVE,
        is_active=False,
        email_verified=False,
    )
    db.add(customer)
    try:
        issue_verification_code(db, email)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    db.refresh(customer)
    return customer


@router.post("/verify-email")
def verify_customer_email(payload: VerificationRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    challenge = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    if challenge is None or not verify_secret(payload.code, challenge.code_hash):
        raise HTTPException(status_code=400, detail="That confirmation code is incorrect. Check the email and try again.")
    expires_at = challenge.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="That confirmation code has expired. Request a new one.")
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None:
        raise HTTPException(status_code=404, detail="No customer account was found for this email.")
    customer.email_verified = True
    customer.is_active = True
    db.delete(challenge)
    db.commit()
    return {"message": "Email confirmed successfully."}


@router.post("/resend-verification")
def resend_customer_verification(payload: ResendVerificationRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER, User.email_verified.is_(False)).first()
    if customer is None:
        raise HTTPException(status_code=404, detail="No unconfirmed customer account was found for this email.")
    issue_verification_code(db, email)
    db.commit()
    return {"message": "A new confirmation code has been sent."}
