from app.models.notifications import NotificationPreference
from app.schemas.chef_settings import ChefNotificationSettings
from app.services.customer_settings import CATEGORY_FIELDS, notification_field, notification_values, settings_response

CHEF_NOTIFICATION_DEFAULTS = ChefNotificationSettings().model_dump()
CHEF_CATEGORY_FIELDS = {category: CATEGORY_FIELDS[category] for category in ("ORDER", "PAYMENT")}


def chef_notification_values(db, chef_id):
    return notification_values(db, chef_id, defaults=CHEF_NOTIFICATION_DEFAULTS,
                               category_fields=CHEF_CATEGORY_FIELDS)


def chef_settings_response(db, chef, **kwargs):
    return settings_response(db, chef, defaults=CHEF_NOTIFICATION_DEFAULTS,
                             category_fields=CHEF_CATEGORY_FIELDS, **kwargs)


def chef_notification_allowed(db, chef, *, notification_type, category, metadata=None):
    if notification_type.startswith(("security.", "account.", "legal.")) or notification_type in {
        "restaurant.suspended", "system.incident", "payment.failed", "wallet.topup.failed",
    }:
        return True
    values = chef_notification_values(db, chef.id)
    field = notification_field(notification_type, category, metadata)
    if field not in values:
        # Categories without a chef-specific switch retain their legacy policy.
        field = None
    if field is None:
        legacy = db.query(NotificationPreference).filter_by(user_id=chef.id, category=category).first()
        if legacy is not None and not legacy.enabled:
            return False
    return values["in_app_enabled"] and (field is None or values[field])
