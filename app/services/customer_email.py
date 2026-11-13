"""Optional customer email delivery uses the same policy as in-app alerts.

Authentication email deliberately continues through core.email directly.
"""

from app.core.email import send_branded_email
from app.services.customer_settings import customer_notification_allowed


def send_customer_notification_email(db, recipient, *, notification_type, category, **message) -> bool:
    if not customer_notification_allowed(db, recipient, notification_type=notification_type, category=category, channel="email"):
        return False
    send_branded_email(to=recipient.email, **message)
    return True
