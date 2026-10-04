import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db.database import Base


class RestaurantFloor(Base):
    __tablename__ = "restaurant_floors"
    __table_args__ = (
        UniqueConstraint("restaurant_id", "floor_number", name="uq_floors_restaurant_number"),
        UniqueConstraint("id", "restaurant_id", name="uq_floors_id_restaurant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False, index=True)
    floor_number: Mapped[int] = mapped_column(Integer, nullable=False)
    floor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    name = synonym("floor_name")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    tables: Mapped[list["DiningTable"]] = relationship(back_populates="floor", cascade="all, delete-orphan")


# Keep the short name used by existing router imports.
Floor = RestaurantFloor


class DiningTable(Base):
    __tablename__ = "dining_tables"
    __table_args__ = (
        UniqueConstraint("floor_id", "table_number", name="uq_dining_tables_floor_number"),
        ForeignKeyConstraint(["floor_id", "restaurant_id"], ["restaurant_floors.id", "restaurant_floors.restaurant_id"], ondelete="RESTRICT", name="fk_dining_tables_floor_restaurant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    restaurant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    floor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    table_number: Mapped[str] = mapped_column(String(30), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    seats = synonym("capacity")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="available")
    table_type: Mapped[str] = mapped_column(String(20), nullable=False, default="round")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    floor: Mapped[RestaurantFloor] = relationship(back_populates="tables")
