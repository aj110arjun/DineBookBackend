"""Shared account settings storage and customer notification eligibility.

The existing customer_settings table has a users FK, so managers can reuse the
same general preferences without a second table or a destructive rename.
"""

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.customer_settings import CustomerSettings
from app.models.notifications import NotificationPreference
from app.models.user import User
from app.schemas.customer_settings import GeneralSettings, NotificationSettings

NOTIFICATION_DEFAULTS = NotificationSettings().model_dump()
CATEGORY_FIELDS = {
    "RESERVATION": ("reservation_confirmation", "reservation_reminders", "reservation_updates"),
    "ORDER": ("order_confirmation", "order_status_updates", "order_completion"),
    "PAYMENT": ("payment_confirmation", "refund_updates", "wallet_transactions"),
    "REVIEW": ("review_updates",),
    "PROMOTION": ("promotional_offers",),
}


def notification_values(db: Session, customer_id, *, defaults=None, category_fields=None) -> dict[str, bool]:
    defaults = NOTIFICATION_DEFAULTS if defaults is None else defaults
    category_fields = CATEGORY_FIELDS if category_fields is None else category_fields
    configured = {row.category: row.enabled for row in db.query(NotificationPreference).filter_by(user_id=customer_id).all()}
    # Preserve existing category opt-outs when granular preferences are first created.
    legacy = {field: configured[category] for category, fields in category_fields.items()
              if category in configured for field in fields}
    return {key: configured.get(key.upper(), legacy.get(key, default))
            for key, default in defaults.items()}


def _initialize_locked(db: Session, customer: User, *, defaults=None, category_fields=None) -> CustomerSettings:
    # The parent exists even before settings do. Lock it to serialize first-use
    # initialization and concurrent PATCHes without a check-then-insert race.
    db.query(User).filter_by(id=customer.id).with_for_update().one()
    row = db.get(CustomerSettings, customer.id)
    if row is None:
        row = CustomerSettings(customer_id=customer.id)
        db.add(row)
    values = notification_values(db, customer.id, defaults=defaults, category_fields=category_fields)
    existing = {pref.category: pref for pref in db.query(NotificationPreference).filter_by(user_id=customer.id).all()}
    for key, value in values.items():
        if key.upper() not in existing:
            db.add(NotificationPreference(user_id=customer.id, category=key.upper(), enabled=value))
    db.flush()
    return row


def settings_response(db: Session, customer: User, *, section: str | None = None, changes: dict | None = None,
                      defaults=None, category_fields=None) -> dict:
    try:
        row = _initialize_locked(db, customer, defaults=defaults, category_fields=category_fields)
        if section == "notifications":
            for key, value in (changes or {}).items():
                pref = db.query(NotificationPreference).filter_by(user_id=customer.id, category=key.upper()).one()
                pref.enabled = value
        elif section == "general":
            for key, value in (changes or {}).items():
                setattr(row, key, value)
        if changes:
            row.updated_at = datetime.now(timezone.utc)
        db.flush()
        result = {"notifications": notification_values(db, customer.id, defaults=defaults, category_fields=category_fields),
                  "general": GeneralSettings.model_validate(row).model_dump()}
        db.commit()
        return result
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(503, "Unable to load or save your settings. Please try again.") from exc


def notification_field(notification_type: str, category: str, metadata: dict | None = None) -> str | None:
    kind = notification_type.lower()
    if kind.startswith(("promotion.", "promotional.", "offer.", "marketing.")) or category == "PROMOTION":
        return "promotional_offers"
    if kind.startswith("reservation."):
        if kind in {"reservation.confirmed", "reservation.confirmation"}:
            return "reservation_confirmation"
        return "reservation_reminders" if "reminder" in kind else "reservation_updates"
    if kind.startswith("order."):
        if kind in {"order.placed", "order.confirmed", "order.confirmation"}:
            return "order_confirmation"
        return "order_completion" if kind in {"order.completed", "order.completion"} else "order_status_updates"
    if "refund" in kind or (metadata or {}).get("transaction_type") == "REFUND":
        return "refund_updates"
    if kind.startswith("wallet."):
        return "wallet_transactions"
    if kind.startswith("payment."):
        return "payment_confirmation"
    if kind.startswith("review.") or category == "REVIEW":
        return "review_updates"
    return None


def customer_notification_allowed(db: Session, customer: User, *, notification_type: str,
                                  category: str, channel: str, metadata: dict | None = None) -> bool:
    # Account protection and required notices have no user-configurable switch.
    if notification_type.startswith(("security.", "account.", "legal.")) or notification_type in {
        "payment.failed", "wallet.topup.failed", "system.incident",
    }:
        return True
    values = notification_values(db, customer.id)
    field = notification_field(notification_type, category.upper(), metadata)
    return values[f"{channel}_enabled"] and (field is None or values[field])
