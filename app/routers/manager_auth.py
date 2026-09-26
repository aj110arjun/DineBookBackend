import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password, verify_secret
from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole


router = APIRouter(
    prefix="/api/auth/manager",
    tags=["manager authentication"],
)

ACCESS_COOKIE = "dinebook_access_token"


class ManagerRegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class ManagerLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


def current_manager(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Please sign in as a manager to continue.",
    )

    if not token:
        raise unauthorized

    try:
        subject = decode_access_token(token).get("sub")
        manager_id = uuid.UUID(subject)

        manager = (
            db.query(User)
            .filter(
                User.id == manager_id,
                User.role == UserRole.MANAGER,
            )
            .first()
        )

    except (ValueError, TypeError):
        raise unauthorized

    if (
        manager is None
        or manager.status != AccountStatus.ACTIVE
        or not manager.is_active
        or not manager.email_verified
    ):
        raise unauthorized

    return manager


@router.post(
    "/register",
    response_model=dict,
    status_code=status.HTTP_201_CREATED,
)
def register_manager(
    payload: ManagerRegisterRequest,
    db: Session = Depends(get_db),
) -> dict:
    email = str(payload.email).strip().lower()

    try:
        existing_user = (
            db.query(User.id)
            .filter(User.email == email)
            .first()
        )
    except ProgrammingError as exc:
        db.rollback()

        if (
            getattr(exc.orig, "sqlstate", None) == "42P01"
            or getattr(exc.orig, "pgcode", None) == "42P01"
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Manager database schema is not initialized. "
                    "From Backend/, run 'venv/bin/alembic upgrade head'."
                ),
            ) from exc

        raise

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    manager = User(
        name=payload.name.strip(),
        email=email,
        password_hash=hash_password(payload.password),
        role=UserRole.MANAGER,
        status=AccountStatus.PENDING,
        is_active=False,
        email_verified=True,
    )

    db.add(manager)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc

    db.refresh(manager)

    return {
        "message": "Manager registration submitted successfully. Your account is waiting for admin approval.",
        "user": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
        },
    }


@router.post("/login", response_model=dict)
def login_manager(
    payload: ManagerLoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    email = str(payload.email).strip().lower()

    manager = (
        db.query(User)
        .filter(
            User.email == email,
            User.role == UserRole.MANAGER,
        )
        .first()
    )

    if manager is None or not verify_secret(
        payload.password,
        manager.password_hash,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    if manager.status == AccountStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your manager registration is still waiting for admin approval.",
        )

    if manager.status == AccountStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your manager registration has been rejected by the admin.",
        )

    if (
        manager.status != AccountStatus.ACTIVE
        or not manager.is_active
        or not manager.email_verified
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your manager account is not active.",
        )

    token, _ = create_access_token(str(manager.id))

    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )

    return {
        "message": "Manager signed in successfully.",
        "user": {
            "id": str(manager.id),
            "name": manager.name,
            "email": manager.email,
            "role": manager.role.value,
            "status": manager.status.value,
        },
    }


@router.get("/me", response_model=dict)
def get_current_manager(
    manager: User = Depends(current_manager),
) -> dict:
    return {
        "id": str(manager.id),
        "name": manager.name,
        "email": manager.email,
        "role": manager.role.value,
        "status": manager.status.value,
    }


@router.post("/logout")
def logout_manager(response: Response) -> dict[str, str]:
    response.delete_cookie(
        key=ACCESS_COOKIE,
        path="/",
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )

    return {
        "message": "Manager signed out successfully.",
    }

