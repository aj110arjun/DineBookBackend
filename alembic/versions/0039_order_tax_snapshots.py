"""Store GST rate and amount on food orders and reservation preorders."""
from alembic import op
import sqlalchemy as sa


revision = "0039_order_tax_snapshots"
down_revision = "0038_calendar_order_date_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("food_orders", sa.Column("tax_percent", sa.Numeric(5, 2), nullable=False, server_default="0.00"))
    op.add_column("food_orders", sa.Column("tax_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"))
    op.add_column("reservations", sa.Column("preorder_tax_percent", sa.Numeric(5, 2), nullable=False, server_default="0.00"))
    op.add_column("reservations", sa.Column("preorder_tax_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"))


def downgrade() -> None:
    op.drop_column("reservations", "preorder_tax_amount")
    op.drop_column("reservations", "preorder_tax_percent")
    op.drop_column("food_orders", "tax_amount")
    op.drop_column("food_orders", "tax_percent")
