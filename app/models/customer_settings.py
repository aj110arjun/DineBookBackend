"""General preferences; notification preferences reuse notification_preferences."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


class CustomerSettings(Base):
    __tablename__ = "customer_settings"
    __table_args__ = (
        CheckConstraint("theme IN ('light', 'dark', 'system')", name="ck_customer_settings_theme"),
        CheckConstraint("language IN ('en')", name="ck_customer_settings_language"),
        CheckConstraint("date_format IN ('DD/MM/YYYY', 'MM/DD/YYYY', 'YYYY-MM-DD')", name="ck_customer_settings_date_format"),
        CheckConstraint("time_format IN ('12h', '24h')", name="ck_customer_settings_time_format"),
    )

    # Legacy table/column names; the users FK supports one record per account.
    # Manager and customer APIs each authorize their own account before access.
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    customer: Mapped["User"] = relationship("User")
    theme: Mapped[str] = mapped_column(String(10), nullable=False, default="system", server_default="system")
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="en", server_default="en")
    date_format: Mapped[str] = mapped_column(String(10), nullable=False, default="DD/MM/YYYY", server_default="DD/MM/YYYY")
    time_format: Mapped[str] = mapped_column(String(3), nullable=False, default="12h", server_default="12h")
    # NULL means follow the browser timezone, never the API server's timezone.
    timezone: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
