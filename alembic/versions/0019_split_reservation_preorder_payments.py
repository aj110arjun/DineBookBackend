"""Track preorder payment separately from required reservation fee payment."""
from alembic import op
import sqlalchemy as sa

revision = "0019_preorder_payments"
down_revision = "0018_food_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("preorder_payment_status", sa.String(20), nullable=True))
    op.add_column("reservations", sa.Column("preorder_payment_method", sa.String(30), nullable=True))


def downgrade() -> None:
    op.drop_column("reservations", "preorder_payment_method")
    op.drop_column("reservations", "preorder_payment_status")
