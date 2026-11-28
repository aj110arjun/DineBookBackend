import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class NotificationCategory(str, Enum):
    RESERVATION = "RESERVATION"
    ORDER = "ORDER"
    PAYMENT = "PAYMENT"
    REVIEW = "REVIEW"
    STAFF = "STAFF"
    APPROVAL = "APPROVAL"
    SYSTEM = "SYSTEM"
    PROMOTION = "PROMOTION"


class NotificationPriority(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class NotificationChannel(str, Enum):
    IN_APP = "in_app"
    EMAIL = "email"


class NotificationType(str, Enum):
    PAYMENT_FAILED = "payment.failed"
    PAYMENT_SUCCESS = "payment.success"
    WALLET_TOPUP_SUCCESS = "wallet.topup.success"
    WALLET_TOPUP_FAILED = "wallet.topup.failed"
    WALLET_TRANSFER_RECEIVED = "wallet.transfer.received"
    WALLET_TRANSACTION = "wallet.transaction"
    WALLET_CREDIT_ISSUED = "wallet.credit.issued"
    RESTAURANT_APPROVAL_REQUESTED = "restaurant.approval_requested"
    RESTAURANT_APPROVED = "restaurant.approved"
    RESTAURANT_REJECTED = "restaurant.rejected"
    RESTAURANT_SUSPENDED = "restaurant.suspended"
    RESTAURANT_REACTIVATED = "restaurant.reactivated"
    SYSTEM_INCIDENT = "system.incident"
    RESERVATION_REMINDER = "reservation.reminder"
    RESERVATION_CONFIRMED = "reservation.confirmed"
    REVIEW_SUBMITTED = "review.submitted"
    REVIEW_FLAGGED = "review.flagged"
    REVIEW_REPLY = "review.reply"
    REVIEW_MODERATED = "review.moderated"


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_recipient_created", "recipient_id", "created_at", "id"),
        Index("ix_notifications_recipient_read", "recipient_id", "is_read"),
        UniqueConstraint("recipient_id", "dedupe_key", name="uq_notifications_recipient_dedupe"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    restaurant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=True)
    notification_type: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default=NotificationPriority.NORMAL.value)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_notification_preferences_user_category"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
