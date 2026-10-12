"""Add Razorpay wallet top-up order records."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0017_wallet_razorpay_topups"
down_revision = "0016_customer_wallet"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_topups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("razorpay_order_id", sa.String(80), nullable=False, unique=True),
        sa.Column("razorpay_payment_id", sa.String(80), nullable=True, unique=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_wallet_topups_user_created", "wallet_topups", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_wallet_topups_user_created", table_name="wallet_topups")
    op.drop_table("wallet_topups")
