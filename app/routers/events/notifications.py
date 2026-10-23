import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.database import get_db
from app.models.notifications import Notification, NotificationPreference
from app.models.user import AccountStatus, User, UserRole
from app.routers.auth.admin_auth import ACCESS_COOKIE, current_admin
from app.routers.auth.chef_auth import current_chef
from app.routers.auth.customer_auth import current_customer
from app.routers.auth.manager_auth import current_manager

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
CATEGORIES = {"RESERVATION", "ORDER", "PAYMENT", "REVIEW", "STAFF", "APPROVAL", "SYSTEM"}


def _user(token: str | None, db: Session) -> User:
    if not token:
        raise HTTPException(401, "Please sign in to continue.")
    try:
        user_id = uuid.UUID(decode_access_token(token).get("sub", ""))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(401, "Please sign in to continue.")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active or user.status != AccountStatus.ACTIVE or not user.email_verified:
        raise HTTPException(401, "Please sign in to continue.")
    try:
        if user.role == UserRole.CUSTOMER:
            current_customer(token=token, db=db)
        elif user.role == UserRole.MANAGER:
            current_manager(token=token, db=db)
        elif user.role == UserRole.CHEF:
            current_chef(token=token, db=db)
        elif user.role == UserRole.ADMIN:
            current_admin(token=token, db=db)
        else:
            raise HTTPException(403, "Notifications are unavailable for this account.")
    except HTTPException:
        raise
    return user


def _serialize(item: Notification) -> dict:
    return {"id": str(item.id), "notification_type": item.notification_type, "category": item.category,
        "title": item.title, "message": item.message, "entity_type": item.entity_type,
        "entity_id": str(item.entity_id) if item.entity_id else None,
        "action_url": item.action_url, "metadata": item.metadata_json or {}, "priority": item.priority,
        "is_read": item.is_read, "read_at": item.read_at.isoformat() if item.read_at else None,
        "created_at": item.created_at.isoformat(), "restaurant_id": str(item.restaurant_id) if item.restaurant_id else None}


@router.get("")
def list_notifications(unread: bool | None = None, category: str | None = None,
                       priority: str | None = None, limit: int = Query(20, ge=1, le=100),
                       offset: int = Query(0, ge=0), token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
                       db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    query = db.query(Notification).filter(Notification.recipient_id == user.id)
    if unread is not None:
        query = query.filter(Notification.is_read.is_(not unread))
    if category:
        category = category.upper()
        if category not in CATEGORIES:
            raise HTTPException(400, "Unknown notification category.")
        query = query.filter(Notification.category == category)
    if priority:
        priority = priority.upper()
        if priority not in {"LOW", "NORMAL", "HIGH", "CRITICAL"}:
            raise HTTPException(400, "Unknown notification priority.")
        query = query.filter(Notification.priority == priority)
    total = query.count()
    items = query.order_by(Notification.created_at.desc(), Notification.id.desc()).offset(offset).limit(limit).all()
    return {"items": [_serialize(item) for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/unread-count")
def unread_count(token: str | None = Cookie(default=None, alias=ACCESS_COOKIE), db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    count = db.query(func.count(Notification.id)).filter(Notification.recipient_id == user.id, Notification.is_read.is_(False)).scalar()
    return {"count": count or 0}


class PreferenceUpdate(BaseModel):
    preferences: dict[str, bool] = Field(max_length=7)


@router.get("/preferences")
def get_preferences(token: str | None = Cookie(default=None, alias=ACCESS_COOKIE), db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    configured = {row.category: row.enabled for row in db.query(NotificationPreference).filter_by(user_id=user.id).all()}
    return {"preferences": {category: configured.get(category, True) for category in sorted(CATEGORIES)}}


@router.patch("/preferences")
def update_preferences(payload: PreferenceUpdate, token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
                       db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    if any(key.upper() not in CATEGORIES for key in payload.preferences):
        raise HTTPException(400, "Unknown notification category.")
    for category, enabled in payload.preferences.items():
        category = category.upper()
        row = db.query(NotificationPreference).filter_by(user_id=user.id, category=category).first()
        if row is None:
            row = NotificationPreference(user_id=user.id, category=category, enabled=enabled)
            db.add(row)
        else:
            row.enabled = enabled
    db.commit()
    return get_preferences(token, db)


@router.patch("/read-all")
def mark_all_read(token: str | None = Cookie(default=None, alias=ACCESS_COOKIE), db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    now = datetime.now(timezone.utc)
    db.query(Notification).filter(Notification.recipient_id == user.id, Notification.is_read.is_(False)).update(
        {Notification.is_read: True, Notification.read_at: now}, synchronize_session=False)
    db.commit()
    return {"message": "Notifications marked as read."}


@router.get("/{notification_id}")
def get_notification(notification_id: uuid.UUID, token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
                     db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    item = db.query(Notification).filter_by(id=notification_id, recipient_id=user.id).first()
    if item is None:
        raise HTTPException(404, "Notification not found.")
    return _serialize(item)


@router.patch("/{notification_id}/read")
def mark_read(notification_id: uuid.UUID, token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
              db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    item = db.query(Notification).filter_by(id=notification_id, recipient_id=user.id).first()
    if item is None:
        raise HTTPException(404, "Notification not found.")
    if not item.is_read:
        item.is_read, item.read_at = True, datetime.now(timezone.utc)
        db.commit()
    return _serialize(item)


@router.delete("/{notification_id}")
def delete_notification(notification_id: uuid.UUID, token: str | None = Cookie(default=None, alias=ACCESS_COOKIE),
                        db: Session = Depends(get_db)) -> dict:
    user = _user(token, db)
    item = db.query(Notification).filter_by(id=notification_id, recipient_id=user.id).first()
    if item is None:
        raise HTTPException(404, "Notification not found.")
    db.delete(item)
    db.commit()
    return {"message": "Notification dismissed."}
