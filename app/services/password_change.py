from datetime import datetime, timezone

from fastapi import HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_secret
from app.models.user import User
from app.schemas.password_change import PasswordChangeRequest


def save_password(
    db: Session,
    account: User,
    password: str,
    response: Response,
    *,
    auth_method: str | None = None,
) -> dict[str, str]:
    account.password_hash = hash_password(password)
    account.password_changed_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, "Unable to update your password. Please try again.") from exc

    token, _ = create_access_token(str(account.id), auth_method=auth_method)
    response.set_cookie(
        key="dinebook_access_token",
        value=token,
        max_age=settings.jwt_expire_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"message": "Your password has been updated."}


def change_password(
    payload: PasswordChangeRequest,
    response: Response,
    account: User,
    db: Session,
) -> dict[str, str]:
    if not verify_secret(payload.current_password, account.password_hash):
        raise HTTPException(400, "Your current password is incorrect.")
    return save_password(db, account, payload.new_password, response)
