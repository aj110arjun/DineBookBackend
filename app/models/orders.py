import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class FoodOrder(Base):
    __tablename__ = "food_orders"
    __table_args__ = (
        UniqueConstraint("customer_id", "idempotency_key", name="uq_food_orders_customer_idempotency_key"),
        CheckConstraint(
            "service_type IN ('DINE_IN', 'TAKEAWAY')",
            name="ck_food_orders_service_type",
        ),
        CheckConstraint(
            "service_type != 'TAKEAWAY' OR (table_id IS NULL AND reservation_id IS NULL)",
            name="ck_food_orders_takeaway_no_visit",
        ),
        Index(
            "uq_food_orders_guest_idempotency_key", "idempotency_key", unique=True,
            postgresql_where=text("customer_id IS NULL"),
            sqlite_where=text("customer_id IS NULL"),
        ),
        Index("ix_food_orders_restaurant_status_created", "restaurant_id", "status", "created_at"),
        Index("ix_food_orders_restaurant_created", "restaurant_id", "created_at"),
        Index("ix_food_orders_restaurant_customer_created", "restaurant_id", "customer_id", "created_at"),
        Index("ix_food_orders_customer_created", "customer_id", "created_at"),
        Index("ix_food_orders_reservation_id", "reservation_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    guest_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    table_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("dining_tables.id", ondelete="SET NULL"), nullable=True)
    service_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="DINE_IN",
        server_default="DINE_IN",
    )
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("food_order_offers.id", ondelete="SET NULL"), nullable=True)
    offer_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    items: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    tax_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("0.00"))
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PLACED")
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False, default="PAY_AT_DESK")
    payment_status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    customer_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preparation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)
    reservation: Mapped["Reservation | None"] = relationship(back_populates="orders")
