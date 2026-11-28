"""Email-bound, single-use proof for the multi-step manager application."""

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.otp import verification_code_expired
from app.models.user import EmailVerificationCode


REGISTRATION_VERIFICATION_LIFETIME = timedelta(hours=2)
TOKEN_HASH_PREFIX = "manager-registration:"


def _token_hash(token: str) -> str:
    return TOKEN_HASH_PREFIX + hashlib.sha256(token.encode()).hexdigest()


def issue_registration_verification(verification: EmailVerificationCode, now: datetime) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = now + REGISTRATION_VERIFICATION_LIFETIME
    verification.code_hash = _token_hash(token)
    verification.expires_at = expires_at
    return token, expires_at


def require_registration_verification(db: Session, email: str, token: str) -> EmailVerificationCode:
    # Keep this row locked until the application commits or rolls back. A
    # successful application deletes it in the same transaction as the account.
    verification = (
        db.query(EmailVerificationCode)
        .filter(EmailVerificationCode.email == email)
        .with_for_update()
        .first()
    )
    if (
        verification is None
        or verification_code_expired(verification.expires_at)
        or not isinstance(token, str)
        or not token
        or not secrets.compare_digest(verification.code_hash, _token_hash(token))
    ):
        raise HTTPException(status_code=403, detail={
            "code": "email_verification_required",
            "message": "Verify your email again before submitting the application.",
        })
    return verification
