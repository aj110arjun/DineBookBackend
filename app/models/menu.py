import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        Index("uq_categories_restaurant_name_active", "restaurant_id", "name", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_categories_restaurant_order", "restaurant_id", "display_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    foods: Mapped[list["Food"]] = relationship(back_populates="category", cascade="all, delete-orphan", passive_deletes=True)


class Food(Base):
    __tablename__ = "food"
    __table_args__ = (Index("ix_food_restaurant_category", "restaurant_id", "category_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_vegetarian: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preparation_time_minutes: Mapped[int | None] = mapped_column(Integer)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    category: Mapped[Category] = relationship(back_populates="foods")
    images: Mapped[list["FoodImage"]] = relationship(back_populates="food", cascade="all, delete-orphan", passive_deletes=True, order_by="FoodImage.display_order")
    variants: Mapped[list["FoodVariant"]] = relationship(back_populates="food", cascade="all, delete-orphan", passive_deletes=True, order_by="FoodVariant.created_at")


class FoodImage(Base):
    __tablename__ = "food_images"
    __table_args__ = (Index("ix_food_images_item_order", "food_item_id", "display_order"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    food_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food.id", ondelete="CASCADE"), nullable=False)
    image_url: Mapped[str] = mapped_column(Text, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    food: Mapped[Food] = relationship(back_populates="images")


class FoodVariant(Base):
    __tablename__ = "food_variants"
    __table_args__ = (Index("ix_food_variants_food", "food_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    food_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("food.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc, onupdate=now_utc)
    food: Mapped[Food] = relationship(back_populates="variants")
