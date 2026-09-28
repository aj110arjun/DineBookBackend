import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password, verify_secret
from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole
from app.schemas.customer import CustomerRegisterRequest, CustomerResponse

router = APIRouter(prefix="/api/auth/customer", tags=["customer authentication"])
session_router = APIRouter(tags=["customer sessions"])
ACCESS_COOKIE = "dinebook_access_token"


class CustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


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
    if not customer.is_active or not customer.email_verified:
        raise HTTPException(status_code=403, detail="Your account is not active.")
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
    )
    db.add(customer)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists.") from exc
    db.refresh(customer)
    return customer
