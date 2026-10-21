"""Add revenue settings and chef share configuration."""
from alembic import op
import sqlalchemy as sa

revision = "0034_revenue_wallets"
down_revision = "0033_reservation_email"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reservations", sa.Column("preorder_paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("food_orders", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("chef_revenue_share_percent", sa.Numeric(5, 2), nullable=False, server_default="0.00"),
    )
    op.create_table(
        "platform_revenue_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("commission_percent", sa.Numeric(5, 2), nullable=False, server_default="10.00"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_platform_revenue_settings_singleton"),
        sa.CheckConstraint("commission_percent >= 0 AND commission_percent <= 100", name="ck_platform_revenue_commission_range"),
    )
    op.execute(sa.text("INSERT INTO platform_revenue_settings (id, commission_percent) VALUES (1, 10.00)"))


def downgrade() -> None:
    op.drop_table("platform_revenue_settings")
    op.drop_column("users", "chef_revenue_share_percent")
    op.drop_column("food_orders", "paid_at")
    op.drop_column("reservations", "preorder_paid_at")
    op.drop_column("reservations", "paid_at")
