"""Track wallet transfers from managers to their chefs."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0035_wallet_transfers"
down_revision = "0034_revenue_wallets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_transfers",
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chef_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["manager_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chef_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("idempotency_key"),
        sa.CheckConstraint("amount > 0", name="ck_wallet_transfer_positive_amount"),
    )
    op.create_index("ix_wallet_transfers_manager_id", "wallet_transfers", ["manager_id"])
    op.create_index("ix_wallet_transfers_chef_id", "wallet_transfers", ["chef_id"])


def downgrade() -> None:
    op.drop_index("ix_wallet_transfers_chef_id", table_name="wallet_transfers")
    op.drop_index("ix_wallet_transfers_manager_id", table_name="wallet_transfers")
    op.drop_table("wallet_transfers")
