import uuid
import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, ForeignKeyConstraint, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.db.database import Base


class DiningTableType(str, enum.Enum):
    ROUND = "round"
    SQUARE = "square"
    RECTANGLE = "rectangle"


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
    reservation_fee: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False, default=250)
    seats = synonym("capacity")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="available")
    table_type: Mapped[DiningTableType] = mapped_column(
        Enum(
            DiningTableType,
            name="dining_table_type",
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        nullable=False,
        default=DiningTableType.ROUND,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    floor: Mapped[RestaurantFloor] = relationship(back_populates="tables")
