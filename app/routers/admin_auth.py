import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, verify_secret
from app.db.database import get_db
from app.models.user import AccountStatus, User, UserRole


router = APIRouter(
    prefix="/api/auth/admin",
    tags=["admin authentication"],
)

ACCESS_COOKIE = "dinebook_access_token"


class AdminLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

@router.post("/login", response_model=dict)
def login_admin(payload: AdminLoginRequest, response: Response, db: Session = Depends(get_db)) -> dict:
    email = str(payload.email).strip().lower()

    admin = (
        db.query(User)
        .filter(
            User.email == email,
            User.role == UserRole.ADMIN,
        )
        .first()
    )

    if admin is None or not verify_secret(payload.password, admin.password_hash):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    if (
        admin.status != AccountStatus.ACTIVE
        or not admin.is_active
        or not admin.email_verified
    ):
        raise HTTPException(
            status_code=403,
            detail="Your admin account is not active.",
        )

    token, _ = create_access_token(str(admin.id))

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
        "message": "Admin signed in successfully.",
        "user": {
            "id": str(admin.id),
            "name": admin.name,
            "email": admin.email,
            "role": admin.role.value,
        },
    }

def current_admin(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=401,
        detail="Please sign in as an admin to continue.",
    )

    if not token:
        raise unauthorized

    try:
        from app.core.security import decode_access_token

        subject = decode_access_token(token).get("sub")
        admin_id = uuid.UUID(subject)

        admin = (
            db.query(User)
            .filter(
                User.id == admin_id,
                User.role == UserRole.ADMIN,
            )
            .first()
        )

    except (ValueError, TypeError):
        raise unauthorized

    if (
        admin is None
        or admin.status != AccountStatus.ACTIVE
        or not admin.is_active
        or not admin.email_verified
    ):
        raise unauthorized

    return admin

@router.get("/me", response_model=dict)
def get_current_admin(
    admin: User = Depends(current_admin),
) -> dict:
    return {
        "id": str(admin.id),
        "name": admin.name,
        "email": admin.email,
        "role": admin.role.value,
    }

@router.post("/logout")
def logout_admin(response: Response) -> dict[str, str]:
    response.delete_cookie(
        key=ACCESS_COOKIE,
        path="/",
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )

    return {
        "message": "Admin signed out successfully."
    }