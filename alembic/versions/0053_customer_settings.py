"""Persist customer general settings; reuse existing notification preference rows."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0053_customer_settings"
down_revision = "0052_customer_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_settings",
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("theme", sa.String(10), nullable=False, server_default="system"),
        sa.Column("language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("date_format", sa.String(10), nullable=False, server_default="DD/MM/YYYY"),
        sa.Column("time_format", sa.String(3), nullable=False, server_default="12h"),
        sa.Column("timezone", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("theme IN ('light', 'dark', 'system')", name="ck_customer_settings_theme"),
        sa.CheckConstraint("language IN ('en')", name="ck_customer_settings_language"),
        sa.CheckConstraint("date_format IN ('DD/MM/YYYY', 'MM/DD/YYYY', 'YYYY-MM-DD')", name="ck_customer_settings_date_format"),
        sa.CheckConstraint("time_format IN ('12h', '24h')", name="ck_customer_settings_time_format"),
    )


def downgrade() -> None:
    op.drop_table("customer_settings")
