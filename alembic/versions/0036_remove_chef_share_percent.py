"""Remove percentage based chef revenue shares."""
from alembic import op


revision = "0036_remove_chef_share_percent"
down_revision = "0035_wallet_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("users", "chef_revenue_share_percent")


def downgrade() -> None:
    import sqlalchemy as sa

    op.add_column(
        "users",
        sa.Column("chef_revenue_share_percent", sa.Numeric(5, 2), server_default="0.00", nullable=False),
    )
