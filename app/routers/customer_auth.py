import uuid
import secrets
import smtplib
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


class CustomerEmailCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class CustomerEmailRequest(BaseModel):
    email: EmailStr


def send_verification_email(email: str, code: str) -> None:
    if not settings.smtp_host or not settings.smtp_from_email:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is not configured. Please contact support.",
        )
    message = EmailMessage()
    message["Subject"] = "Your DineBook confirmation code"
    message["From"] = settings.smtp_from_email
    message["To"] = email
    message.set_content(
        f"Your DineBook confirmation code is {code}. It expires in 10 minutes."
    )
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password or "")
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We couldn’t send the confirmation email. Please try again.",
        ) from exc


def issue_verification_code(db: Session, email: str) -> None:
    code = f"{secrets.randbelow(1_000_000):06d}"
    record = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    if record is None:
        record = EmailVerificationCode(email=email, code_hash=hash_password(code), expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
        db.add(record)
    else:
        record.code_hash = hash_password(code)
        record.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db.flush()
    send_verification_email(email, code)


def current_customer(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(status_code=401, detail="Please sign in to continue.")
    if not token:
        raise unauthorized
    try:
        customer_id = uuid.UUID(decode_access_token(token).get("sub"))
    except (ValueError, TypeError, AttributeError):
        raise unauthorized
    customer = db.query(User).filter(User.id == customer_id, User.role == UserRole.CUSTOMER).first()
    if customer is None or not customer.is_active or not customer.email_verified:
        raise unauthorized
    return customer


@router.post("/login", response_model=CustomerResponse)
def login_customer(payload: CustomerLoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None or not verify_secret(payload.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not customer.is_active:
        raise HTTPException(status_code=403, detail="Your account is not active.")
    if not customer.email_verified:
        raise HTTPException(status_code=403, detail="Confirm your email address before signing in.")
    token, _ = create_access_token(str(customer.id))
    response.set_cookie(ACCESS_COOKIE, token, max_age=settings.jwt_expire_minutes * 60, httponly=True, secure=settings.auth_cookie_secure, samesite="lax", path="/")
    return customer


@session_router.get("/api/customer/me", response_model=CustomerResponse)
def get_current_customer(customer: User = Depends(current_customer)) -> User:
    return customer


@session_router.post("/api/auth/logout")
def logout_customer(response: Response) -> dict[str, str]:
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, secure=settings.auth_cookie_secure, samesite="lax")
    return {"message": "Signed out successfully."}


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
        is_active=True,
        email_verified=False,
    )
    db.add(customer)
    try:
        issue_verification_code(db, email)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    db.refresh(customer)
    return customer


@router.post("/verify-email")
def verify_customer_email(payload: CustomerEmailCodeRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No customer account was found for this email.")
    if customer.email_verified:
        return {"message": "Email address is already confirmed."}
    verification = db.query(EmailVerificationCode).filter(EmailVerificationCode.email == email).first()
    now = datetime.now(timezone.utc)
    if verification is None or verification.expires_at.replace(tzinfo=timezone.utc) <= now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That confirmation code has expired. Request a new code.")
    if not verify_secret(payload.code, verification.code_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That confirmation code is incorrect.")
    customer.email_verified = True
    db.delete(verification)
    db.commit()
    return {"message": "Email address confirmed."}


@router.post("/resend-verification")
def resend_customer_verification(payload: CustomerEmailRequest, db: Session = Depends(get_db)) -> dict[str, str]:
    email = str(payload.email).strip().lower()
    customer = db.query(User).filter(User.email == email, User.role == UserRole.CUSTOMER).first()
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No customer account was found for this email.")
    if customer.email_verified:
        return {"message": "Email address is already confirmed."}
    try:
        issue_verification_code(db, email)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    return {"message": "A new confirmation code has been sent."}
