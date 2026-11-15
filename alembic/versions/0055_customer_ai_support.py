"""Customer-only AI conversations and versioned, multilingual FAQ retrieval."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from app.customer_ai_seed_v1 import KNOWLEDGE_V1

revision = "0055_customer_ai_support"
down_revision = "0054_manager_dashboard_indexes"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("customer_ai_conversations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False), sa.Column("language", sa.String(8), nullable=False),
        sa.Column("lease_token", sa.String(36)), sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_customer_ai_owner_updated", "customer_ai_conversations", ["user_id", "updated_at"])
    op.create_table("customer_ai_turns",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("customer_ai_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_message_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(12), nullable=False), sa.Column("error_code", sa.String(40)),
        sa.Column("tool_metadata", sa.JSON(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "client_message_id", name="uq_customer_ai_turn_client"),
        sa.CheckConstraint("status IN ('running','complete','failed','cancelled')", name="ck_customer_ai_turn_status"))
    op.create_index("ix_customer_ai_turns_conversation_id", "customer_ai_turns", ["conversation_id"])
    op.create_table("customer_ai_messages",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("customer_ai_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", UUID(as_uuid=True), sa.ForeignKey("customer_ai_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender", sa.String(12), nullable=False), sa.Column("content", sa.Text(), nullable=False),
        sa.Column("answer", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("turn_id", "sender", name="uq_customer_ai_message_sender"),
        sa.CheckConstraint("sender IN ('user','assistant')", name="ck_customer_ai_message_sender"))
    op.create_index("ix_customer_ai_message_history", "customer_ai_messages", ["conversation_id", "created_at"])
    table = op.create_table("customer_ai_knowledge",
        sa.Column("key", sa.String(60), primary_key=True), sa.Column("title", sa.JSON(), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False), sa.Column("keywords", sa.JSON(), nullable=False),
        sa.Column("href", sa.String(240), nullable=False), sa.Column("version", sa.String(20), nullable=False))
    op.bulk_insert(table, [dict(row, version="1") for row in KNOWLEDGE_V1])


def downgrade():
    op.drop_table("customer_ai_knowledge")
    op.drop_index("ix_customer_ai_message_history", table_name="customer_ai_messages")
    op.drop_table("customer_ai_messages")
    op.drop_index("ix_customer_ai_turns_conversation_id", table_name="customer_ai_turns")
    op.drop_table("customer_ai_turns")
    op.drop_index("ix_customer_ai_owner_updated", table_name="customer_ai_conversations")
    op.drop_table("customer_ai_conversations")
