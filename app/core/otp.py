from datetime import datetime, timezone


def verification_code_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    """Compare database timestamps safely whether the driver returns them aware or naive."""
    current_time = now or datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at <= current_time.astimezone(timezone.utc)
