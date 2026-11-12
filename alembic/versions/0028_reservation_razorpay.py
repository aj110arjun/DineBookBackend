"""Store Razorpay references for reservation fee payments."""
from alembic import op
import sqlalchemy as sa

revision = "0028_reservation_razorpay"
down_revision = "0027_operating_hours_exceptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("razorpay_order_id", sa.String(length=80), nullable=True))
    op.add_column("reservations", sa.Column("razorpay_payment_id", sa.String(length=80), nullable=True))
    op.create_unique_constraint("uq_reservations_razorpay_order_id", "reservations", ["razorpay_order_id"])
    op.create_unique_constraint("uq_reservations_razorpay_payment_id", "reservations", ["razorpay_payment_id"])


def downgrade() -> None:
    op.drop_constraint("uq_reservations_razorpay_payment_id", "reservations", type_="unique")
    op.drop_constraint("uq_reservations_razorpay_order_id", "reservations", type_="unique")
    op.drop_column("reservations", "razorpay_payment_id")
    op.drop_column("reservations", "razorpay_order_id")
