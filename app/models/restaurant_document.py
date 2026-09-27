import enum
import uuid

from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class RestaurantDocumentType(str, enum.Enum):
    FSSAI_LICENSE = "FSSAI_LICENSE"
    BUSINESS_REGISTRATION = "BUSINESS_REGISTRATION"
    GST_CERTIFICATE = "GST_CERTIFICATE"
    OWNER_IDENTITY = "OWNER_IDENTITY"
    BRANDING_IMAGE = "BRANDING_IMAGE"


class RestaurantDocument(Base):
    __tablename__ = "restaurant_documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )

    restaurant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("restaurants.id", ondelete="CASCADE"),
        nullable=False,
    )

    document_type: Mapped[RestaurantDocumentType] = mapped_column(
        Enum(
            RestaurantDocumentType,
            name="restaurant_document_type",
        ),
        nullable=False,
    )

    file_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )