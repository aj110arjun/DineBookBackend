import uuid
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


def now_utc():
    return datetime.now(timezone.utc)


class AITurnStatus(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AIMessageSender(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class CustomerAIConversation(Base):
    __tablename__ = "customer_ai_conversations"
    __table_args__ = (Index("ix_customer_ai_owner_updated", "user_id", "updated_at"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(100), nullable=False, default="Customer support")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="auto")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class CustomerAITurn(Base):
    __tablename__ = "customer_ai_turns"
    __table_args__ = (
        UniqueConstraint("conversation_id", "client_message_id", name="uq_customer_ai_turn_client"),
        CheckConstraint("status IN ('running','complete','failed','cancelled')", name="ck_customer_ai_turn_status"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    client_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default=AITurnStatus.RUNNING.value)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tool_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class CustomerAIMessage(Base):
    __tablename__ = "customer_ai_messages"
    __table_args__ = (
        Index("ix_customer_ai_message_history", "conversation_id", "created_at"),
        UniqueConstraint("turn_id", "sender", name="uq_customer_ai_message_sender"),
        CheckConstraint("sender IN ('user','assistant')", name="ck_customer_ai_message_sender"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_ai_conversations.id", ondelete="CASCADE"), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_ai_turns.id", ondelete="CASCADE"), nullable=False)
    sender: Mapped[str] = mapped_column(String(12), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=now_utc)


class CustomerAIKnowledge(Base):
    __tablename__ = "customer_ai_knowledge"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    title: Mapped[dict] = mapped_column(JSON, nullable=False)
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    keywords: Mapped[list] = mapped_column(JSON, nullable=False)
    href: Mapped[str] = mapped_column(String(240), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False, default="1")
