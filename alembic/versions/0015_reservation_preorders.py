"""Store preorder details and reservation checkout method."""
from alembic import op
import sqlalchemy as sa

revision = "0015_reservation_preorders"
down_revision = "0014_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("fulfillment_type", sa.String(length=20), nullable=False, server_default="TABLE_ONLY"))
    op.add_column("reservations", sa.Column("payment_method", sa.String(length=30), nullable=False, server_default="PAY_AT_DESK"))
    op.add_column("reservations", sa.Column("preorder_items", sa.JSON(), nullable=True))
    op.add_column("reservations", sa.Column("preorder_total", sa.Numeric(10, 2), nullable=False, server_default="0.00"))


def downgrade() -> None:
    op.drop_column("reservations", "preorder_total")
    op.drop_column("reservations", "preorder_items")
    op.drop_column("reservations", "payment_method")
    op.drop_column("reservations", "fulfillment_type")
