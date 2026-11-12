"""Record platform commission accruals and manager initiated payouts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0037_platform_commission_payouts"
down_revision = "0036_remove_chef_share_percent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "platform_commission_payouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("manager_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("admin_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_platform_commission_payouts_manager_id", "platform_commission_payouts", ["manager_id"])
    op.create_index("ix_platform_commission_payouts_admin_id", "platform_commission_payouts", ["admin_id"])
    op.create_table(
        "platform_commissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("manager_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("gross_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("commission_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("commission_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payout_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("platform_commission_payouts.id", ondelete="RESTRICT"), nullable=True),
        sa.UniqueConstraint("source_type", "source_id", name="uq_platform_commissions_source"),
    )
    op.create_index("ix_platform_commissions_manager_id", "platform_commissions", ["manager_id"])
    op.create_index("ix_platform_commissions_restaurant_id", "platform_commissions", ["restaurant_id"])
    op.create_index("ix_platform_commissions_payout_id", "platform_commissions", ["payout_id"])


def downgrade() -> None:
    op.drop_table("platform_commissions")
    op.drop_table("platform_commission_payouts")
