import uuid

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, decode_access_token, hash_password, verify_secret
from app.db.database import get_db
from app.models.restaurant import Restaurant
from app.models.user import AccountStatus, User, UserRole


router = APIRouter(prefix="/api/auth/chef", tags=["chef authentication"])
session_router = APIRouter(tags=["chef sessions"])
ACCESS_COOKIE = "dinebook_access_token"


class ChefLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChefChangePasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


def current_chef(
    token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Please sign in as a chef to continue.",
    )
    if not token:
        raise unauthorized

    try:
        subject = decode_access_token(token).get("sub")
        chef_id = uuid.UUID(subject)
    except (ValueError, TypeError):
        raise unauthorized

    chef = (
        db.query(User)
        .filter(User.id == chef_id, User.role == UserRole.CHEF)
        .first()
    )
    if (
        chef is None
        or chef.status != AccountStatus.ACTIVE
        or not chef.is_active
        or not chef.email_verified
    ):
        raise unauthorized
    return chef


def chef_profile(chef: User, db: Session | None = None) -> dict:
    restaurant = None
    if db is not None and chef.manager_id is not None:
        restaurant = db.query(Restaurant.name).filter(Restaurant.manager_id == chef.manager_id).scalar()
    return {
        "id": str(chef.id),
        "name": chef.name,
        "email": chef.email,
        "role": chef.role.value,
        "status": chef.status.value,
        "restaurant_name": restaurant,
        "must_change_password": chef.must_change_password,
    }


@router.post("/login", response_model=dict)
def login_chef(
    payload: ChefLoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    email = str(payload.email).strip().lower()
    chef = (
        db.query(User)
        .filter(User.email == email, User.role == UserRole.CHEF)
        .first()
    )

    if chef is None or not verify_secret(payload.password, chef.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    if chef.status == AccountStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your chef account is waiting for approval.",
        )
    if chef.status == AccountStatus.REJECTED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your chef account has been rejected.",
        )
    if (
        chef.status != AccountStatus.ACTIVE
        or not chef.is_active
        or not chef.email_verified
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your chef account is not active.",
        )

    token, _ = create_access_token(str(chef.id))
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
        "message": "Chef signed in successfully.",
        "user": chef_profile(chef, db),
        "must_change_password": chef.must_change_password,
    }


@router.post("/change-password", response_model=dict)
def change_chef_password(
    payload: ChefChangePasswordRequest,
    response: Response,
    chef: User = Depends(current_chef),
    db: Session = Depends(get_db),
) -> dict:
    chef.password_hash = hash_password(payload.password)
    chef.must_change_password = False
    db.commit()
    token, _ = create_access_token(str(chef.id))
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"message": "Your password has been updated."}


@session_router.get("/api/chef/me", response_model=dict)
def get_current_chef(
    chef: User = Depends(current_chef), db: Session = Depends(get_db)
) -> dict:
    return chef_profile(chef, db)


@router.post("/logout", response_model=dict)
def logout_chef(response: Response) -> dict[str, str]:
    response.delete_cookie(
        key=ACCESS_COOKIE,
        path="/",
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
    )
    return {"message": "Chef signed out successfully."}
