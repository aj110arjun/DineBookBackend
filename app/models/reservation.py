import uuid
from datetime import date, datetime, time, timezone
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, Time
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class Reservation(Base):
    __tablename__ = "reservations"
    __table_args__ = (
        Index("ix_reservations_restaurant_date_status", "restaurant_id", "reservation_date", "status"),
        Index("ix_reservations_user_date", "user_id", "reservation_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    reservation_date: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    number_of_guests: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING_PAYMENT")
    special_request: Mapped[str | None] = mapped_column(Text)
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    razorpay_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True)
    preorder_payment_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preorder_payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    fulfillment_type: Mapped[str] = mapped_column(String(20), nullable=False, default="TABLE_ONLY")
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False, default="PAY_AT_DESK")
    preorder_items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    preorder_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    preorder_discount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    preorder_offer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("food_order_offers.id", ondelete="SET NULL"), nullable=True)
    preorder_offer_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_email_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    assignments: Mapped[list["ReservationTable"]] = relationship(back_populates="reservation", cascade="all, delete-orphan")
    orders: Mapped[list["FoodOrder"]] = relationship(back_populates="reservation")


class ReservationTable(Base):
    __tablename__ = "reservation_tables"
    __table_args__ = (Index("ix_reservation_tables_table", "table_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False)
    table_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("dining_tables.id", ondelete="RESTRICT"), nullable=False)
    reservation: Mapped[Reservation] = relationship(back_populates="assignments")
    table: Mapped["DiningTable"] = relationship()


from app.models.dining import DiningTable  # noqa: E402
