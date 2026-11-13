from app.schemas.manager_settings import ManagerNotificationSettings
from app.models.notifications import NotificationPreference
from app.services.customer_settings import CATEGORY_FIELDS, notification_field, notification_values, settings_response

MANAGER_NOTIFICATION_DEFAULTS = ManagerNotificationSettings().model_dump()
MANAGER_CATEGORY_FIELDS = {
    **{category: fields for category, fields in CATEGORY_FIELDS.items() if category != "PROMOTION"},
    "STAFF": ("staff_updates",),
    "APPROVAL": ("approval_updates",),
}


def manager_notification_values(db, manager_id):
    return notification_values(db, manager_id, defaults=MANAGER_NOTIFICATION_DEFAULTS,
                               category_fields=MANAGER_CATEGORY_FIELDS)


def manager_settings_response(db, manager, **kwargs):
    return settings_response(db, manager, defaults=MANAGER_NOTIFICATION_DEFAULTS,
                             category_fields=MANAGER_CATEGORY_FIELDS, **kwargs)


def manager_notification_allowed(db, manager, *, notification_type, category, metadata=None):
    # Authentication, account protection, failed payments and restaurant
    # suspension remain actionable even when optional notifications are paused.
    if notification_type.startswith(("security.", "account.", "legal.")) or notification_type in {
        "restaurant.suspended", "system.incident", "payment.failed", "wallet.topup.failed",
    }:
        return True
    values = manager_notification_values(db, manager.id)
    field = notification_field(notification_type, category, metadata)
    if category in {"STAFF", "APPROVAL"}:
        field = MANAGER_CATEGORY_FIELDS[category][0]
    if field is not None and field not in values:
        # Managers do not subscribe to customer promotional campaigns.
        return False
    if field is None:
        legacy = db.query(NotificationPreference).filter_by(user_id=manager.id, category=category).first()
        if legacy is not None and not legacy.enabled:
            return False
    return values["in_app_enabled"] and (field is None or values[field])
