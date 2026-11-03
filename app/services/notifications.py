import uuid
from urllib.parse import urlsplit

from decimal import Decimal
from sqlalchemy.orm import Session

from app.models.notifications import Notification, NotificationPreference
from app.models.realtime import RealtimeEvent
from app.models.restaurant import Restaurant
from app.models.user import User, UserRole
from app.models.wallet import WalletTransaction

ALLOWED_CATEGORIES = {"RESERVATION", "ORDER", "PAYMENT", "REVIEW", "STAFF", "APPROVAL", "SYSTEM"}
MANDATORY_TYPES = {"payment.failed", "payment.success", "wallet.topup.success", "wallet.topup.failed",
                   "restaurant.suspended", "system.incident", "reservation.reminder", "review.moderated",
                   "wallet.transaction", "wallet.credit.issued",}
ALLOWED_ACTIONS = {
    "/customer/profile/orders", "/customer/profile/reservations", "/customer/profile/wallet",
    "/customer/profile/reviews", "/manager/orders", "/manager/reservations", "/manager/reviews",
    "/manager/dashboard", "/manager/pending", "/chef/orders", "/admin/reviews", "/admin/requests",
    "/manager/revenue", "/chef/revenue", "/admin/revenue",
}


def _safe_action_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    path = parsed.path
    allowed = path in ALLOWED_ACTIONS or (
        path.startswith("/customer/profile/reservations/") and path.count("/") == 4
    )
    if parsed.scheme or parsed.netloc or ".." in path.split("/") or not allowed:
        return None
    return value[:500]


def create_notification(db: Session, recipient: User, *, notification_type: str, category: str,
                        title: str, message: str, restaurant_id: uuid.UUID | None = None,
                        entity_type: str | None = None, entity_id: uuid.UUID | None = None,
                        action_url: str | None = None, priority: str = "NORMAL",
                        dedupe_key: str | None = None, metadata: dict | None = None) -> Notification | None:
    """Persist a safe notification and a recipient scoped wake-up in the caller's transaction."""
    category = category.upper()
    if category not in ALLOWED_CATEGORIES:
        raise ValueError("Unsupported notification category")
    action_url = _safe_action_url(action_url)
    if dedupe_key and db.query(Notification.id).filter_by(recipient_id=recipient.id, dedupe_key=dedupe_key).first():
        return None
    pref = db.query(NotificationPreference).filter_by(user_id=recipient.id, category=category).first()
    if pref is not None and not pref.enabled and notification_type not in MANDATORY_TYPES:
        return None
    item = Notification(
        recipient_id=recipient.id, restaurant_id=restaurant_id, notification_type=notification_type[:80],
        category=category, title=title.strip()[:160], message=message.strip()[:2000],
        entity_type=entity_type[:60] if entity_type else None, entity_id=entity_id,
        action_url=action_url, priority=priority,
        dedupe_key=dedupe_key[:160] if dedupe_key else None,
        metadata_json=metadata or {},
    )
    db.add(item)
    db.flush()
    # Notification wake-ups are strictly user scoped. Restaurant scope here would
    # make the SSE fan-out include the user's notification event for other staff.
    db.add(RealtimeEvent(user_id=recipient.id,
        event_type="notification.created", resource_id=str(item.id),
        data={"notification_id": str(item.id)}))
    return item


def notify_order_status(db: Session, order, previous_status: str | None = None) -> None:
    restaurant = db.query(Restaurant).filter_by(id=order.restaurant_id).first()
    customer = db.query(User).filter_by(id=order.customer_id).first()
    if customer:
        status = str(order.status)
        create_notification(db, customer, notification_type=f"order.{status.lower()}", category="ORDER",
            title=f"Order {status.lower().replace('_', ' ')}", message=f"Your order status is now {status.lower().replace('_', ' ')}.",
            restaurant_id=order.restaurant_id, entity_type="order", entity_id=order.id,
            action_url="/customer/profile/orders", dedupe_key=f"order:{order.id}:{status}")
    if restaurant:
        recipients = db.query(User).filter(User.id == restaurant.manager_id).all()
        recipients += db.query(User).filter(User.manager_id == restaurant.manager_id, User.role == UserRole.CHEF).all()
        for recipient in recipients:
            create_notification(db, recipient, notification_type=f"order.{str(order.status).lower()}", category="ORDER",
                title="Order status updated", message=f"An order is now {str(order.status).lower().replace('_', ' ')}.",
                restaurant_id=order.restaurant_id, entity_type="order", entity_id=order.id,
                action_url="/chef/orders" if recipient.role == UserRole.CHEF else "/manager/orders",
                dedupe_key=f"order:{order.id}:{order.status}:staff:{recipient.id}")


def notify_reservation_status(db: Session, reservation) -> None:
    status = str(reservation.status)
    customer = db.query(User).filter_by(id=reservation.user_id).first()
    if customer:
        create_notification(db, customer, notification_type=f"reservation.{status.lower()}", category="RESERVATION",
            title=f"Reservation {status.lower().replace('_', ' ')}", message=f"Your reservation status is now {status.lower().replace('_', ' ')}.",
            restaurant_id=reservation.restaurant_id, entity_type="reservation", entity_id=reservation.id,
            action_url=f"/customer/profile/reservations/{reservation.id}",
            dedupe_key=f"reservation:{reservation.id}:{status}")
    restaurant = db.query(Restaurant).filter_by(id=reservation.restaurant_id).first()
    manager = db.query(User).filter_by(id=restaurant.manager_id).first() if restaurant else None
    if manager:
        create_notification(db, manager, notification_type=f"reservation.{status.lower()}", category="RESERVATION",
            title=f"Reservation {status.lower().replace('_', ' ')}", message="A restaurant reservation was updated.",
            restaurant_id=reservation.restaurant_id, entity_type="reservation", entity_id=reservation.id,
            action_url="/manager/reservations", dedupe_key=f"reservation:{reservation.id}:{status}:manager")

def notify_wallet_transaction(
    db: Session,
    transaction: WalletTransaction,
) -> None:
    recipient = db.query(User).filter(
        User.id == transaction.user_id
    ).first()

    if recipient is None:
        return

    # Ensure the transaction has an ID before constructing the notification.
    db.flush()

    action_urls = {
        UserRole.CUSTOMER: "/customer/profile/wallet",
        UserRole.MANAGER: "/manager/revenue",
        UserRole.CHEF: "/chef/revenue",
        UserRole.ADMIN: "/admin/revenue",
    }

    amount = abs(Decimal(transaction.amount))
    is_refund = transaction.transaction_type == "REFUND"
    is_credit = transaction.transaction_type in {"CREDIT", "REFUND"}
    movement = "credited to" if is_credit else "debited from"
    title = "Wallet refund received" if is_refund else ("Wallet credited" if is_credit else "Wallet debited")
    message = (
        f"₹{amount:,.2f} was refunded to your wallet. {transaction.description}"
        if is_refund else
        f"₹{amount:,.2f} was {movement} your wallet. {transaction.description}"
    )

    create_notification(
        db,
        recipient,
        notification_type="wallet.transaction",
        category="PAYMENT",
        title=title,
        message=message,
        entity_type="wallet_transaction",
        entity_id=transaction.id,
        action_url=action_urls.get(recipient.role),
        dedupe_key=f"wallet-transaction:{transaction.id}",
        metadata={
            "transaction_id": str(transaction.id),
            "amount": str(transaction.amount),
            "transaction_type": transaction.transaction_type,
        },
    )
