from app.models.notifications import NotificationPreference
from app.schemas.admin_settings import AdminNotificationSettings
from app.services.customer_settings import (
    notification_values,
    settings_response,
)


ADMIN_NOTIFICATION_DEFAULTS = AdminNotificationSettings().model_dump()

ADMIN_CATEGORY_FIELDS = {
    "APPROVAL": ("approval_updates",),
    "REVIEW": ("review_updates",),
    "PAYMENT": ("wallet_transactions",),
    "SYSTEM": ("system_updates",),
}

ESSENTIAL_ADMIN_TYPES = {
    "payment.failed",
    "wallet.topup.failed",
    "restaurant.suspended",
    "system.incident",
}


def admin_notification_values(db, admin_id):
    return notification_values(
        db,
        admin_id,
        defaults=ADMIN_NOTIFICATION_DEFAULTS,
        category_fields=ADMIN_CATEGORY_FIELDS,
    )


def admin_settings_response(db, admin, **kwargs):
    return settings_response(
        db,
        admin,
        defaults=ADMIN_NOTIFICATION_DEFAULTS,
        category_fields=ADMIN_CATEGORY_FIELDS,
        **kwargs,
    )


def admin_notification_allowed(
    db,
    admin,
    *,
    notification_type,
    category,
    metadata=None,
):
    # Essential notices bypass optional notification preferences.
    if (
        notification_type.startswith(("security.", "account.", "legal."))
        or notification_type in ESSENTIAL_ADMIN_TYPES
    ):
        return True

    category = category.upper()
    values = admin_notification_values(db, admin.id)

    if not values["in_app_enabled"]:
        return False

    fields = ADMIN_CATEGORY_FIELDS.get(category)

    if fields:
        return all(values[field] for field in fields)

    # Preserve preferences for any other existing notification category.
    legacy = (
        db.query(NotificationPreference)
        .filter_by(user_id=admin.id, category=category)
        .first()
    )

    return legacy is None or legacy.enabled
