import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class FoodOrderOffer(Base):
    __tablename__ = "food_order_offers"
    __table_args__ = (
        CheckConstraint("discount_percent > 0", name="ck_food_order_offer_discount_positive"),
        CheckConstraint("minimum_subtotal >= 0", name="ck_food_order_offer_minimum_nonnegative"),
        CheckConstraint("uses >= 0 AND (max_uses IS NULL OR (max_uses > 0 AND uses <= max_uses))", name="ck_food_order_offer_uses_within_limit"),
        CheckConstraint("discount_type IN ('PERCENTAGE', 'FIXED')", name="ck_food_order_offer_discount_type"),
        CheckConstraint("applicability IN ('RESTAURANT', 'ITEMS', 'CATEGORIES')", name="ck_food_order_offer_applicability"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="Offer")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_type: Mapped[str] = mapped_column(String(16), nullable=False, default="PERCENTAGE")
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    max_discount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    minimum_subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    per_customer_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applicability: Mapped[str] = mapped_column(String(16), nullable=False, default="RESTAURANT")
    eligible_foods: Mapped[list["OfferEligibleFood"]] = relationship(back_populates="offer", cascade="all, delete-orphan", passive_deletes=True)
    eligible_categories: Mapped[list["OfferEligibleCategory"]] = relationship(back_populates="offer", cascade="all, delete-orphan", passive_deletes=True)
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)

    @property
    def eligible_food_ids(self) -> list[str]:
        return [str(link.food_id) for link in self.eligible_foods]

    @eligible_food_ids.setter
    def eligible_food_ids(self, values: list[str]) -> None:
        self.eligible_foods = [OfferEligibleFood(food_id=uuid.UUID(str(value))) for value in values]

    @property
    def eligible_category_ids(self) -> list[str]:
        return [str(link.category_id) for link in self.eligible_categories]

    @eligible_category_ids.setter
    def eligible_category_ids(self, values: list[str]) -> None:
        self.eligible_categories = [OfferEligibleCategory(category_id=uuid.UUID(str(value))) for value in values]


class OfferEligibleFood(Base):
    __tablename__ = "offer_eligible_foods"
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food_order_offers.id", ondelete="CASCADE"), primary_key=True)
    food_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food.id", ondelete="CASCADE"), primary_key=True)
    offer: Mapped[FoodOrderOffer] = relationship(back_populates="eligible_foods")


class OfferEligibleCategory(Base):
    __tablename__ = "offer_eligible_categories"
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food_order_offers.id", ondelete="CASCADE"), primary_key=True)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True)
    offer: Mapped[FoodOrderOffer] = relationship(back_populates="eligible_categories")


class OfferRedemption(Base):
    __tablename__ = "offer_redemptions"
    __table_args__ = (UniqueConstraint("order_id", name="uq_offer_redemptions_order"),
                      Index("ix_offer_redemptions_offer_customer", "offer_id", "customer_id"))
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food_order_offers.id", ondelete="RESTRICT"), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food_orders.id", ondelete="RESTRICT"), nullable=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="APPLIED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class FoodOrder(Base):
    __tablename__ = "food_orders"
    __table_args__ = (
        UniqueConstraint("customer_id", "idempotency_key", name="uq_food_orders_customer_idempotency_key"),
        Index("ix_food_orders_restaurant_status_created", "restaurant_id", "status", "created_at"),
        Index("ix_food_orders_restaurant_created", "restaurant_id", "created_at"),
        Index("ix_food_orders_customer_created", "customer_id", "created_at"),
        Index("ix_food_orders_reservation_id", "reservation_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    guest_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    table_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("dining_tables.id", ondelete="SET NULL"), nullable=True)
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False)
    offer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("food_order_offers.id", ondelete="SET NULL"), nullable=True)
    offer_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    items: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
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
