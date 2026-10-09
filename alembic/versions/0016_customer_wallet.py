"""Add customer wallet balances and transaction ledger."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_customer_wallet"
down_revision = "0015_reservation_preorders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wallet_accounts",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("balance", sa.Numeric(12, 2), nullable=False, server_default="0.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "wallet_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("reservations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("transaction_type", sa.String(20), nullable=False),
        sa.Column("description", sa.String(240), nullable=False),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_wallet_transactions_user_created", "wallet_transactions", ["user_id", "created_at"])
    op.create_index("ix_wallet_transactions_reservation", "wallet_transactions", ["reservation_id"])


def downgrade() -> None:
    op.drop_index("ix_wallet_transactions_reservation", table_name="wallet_transactions")
    op.drop_index("ix_wallet_transactions_user_created", table_name="wallet_transactions")
    op.drop_table("wallet_transactions")
    op.drop_table("wallet_accounts")
