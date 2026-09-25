import secrets
import smtplib
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password, verify_secret
from app.db.database import get_db
from app.models.user import AccountStatus, EmailVerificationCode, User, UserRole
from app.schemas.customer import CustomerRegisterRequest, CustomerResponse

router = APIRouter(prefix="/api/auth/customer", tags=["customer authentication"])
session_router = APIRouter(tags=["customer sessions"])
ACCESS_COOKIE = "dinebook_access_token"


class CustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class VerificationRequest(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")


class ResendVerificationRequest(BaseModel):
    email: EmailStr


def current_customer(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(status_code=401, detail="Please sign in to continue.")
    if not token:
        raise unauthorized
    try:
        subject = decode_access_token(token).get("sub")
        customer_id = uuid.UUID(subject)
        customer = db.query(User).filter(User.id == customer_id, User.role == UserRole.CUSTOMER).first()
    except (ValueError, TypeError):
        raise unauthorized
    if customer is None or not customer.is_active or not customer.email_verified:
        raise unauthorized
    return customer


@router.post("/login", response_model=CustomerResponse)
def login_customer(payload: CustomerLoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None or not verify_secret(payload.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not customer.email_verified or not customer.is_active:
        raise HTTPException(status_code=403, detail="Please confirm your email address before signing in.")
    token, _ = create_access_token(str(customer.id))
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return customer


@session_router.get("/api/customer/me", response_model=CustomerResponse)
def get_current_customer(customer: User = Depends(current_customer)) -> User:
    return customer


@session_router.post("/api/auth/logout")
def logout_customer(response: Response) -> dict[str, str]:
    response.delete_cookie(key=ACCESS_COOKIE, path="/", httponly=True, secure=settings.auth_cookie_secure, samesite="lax")
    return {"message": "Signed out successfully."}


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
