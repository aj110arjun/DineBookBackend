"""Add manager-set hourly table charges and booked fee snapshots."""

from alembic import op
import sqlalchemy as sa


revision = "0049_reservation_extra_hours"
down_revision = "0048_order_service_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dining_tables", sa.Column("extra_hour_fee", sa.Numeric(10, 2), nullable=False, server_default="0.00"))
    op.create_check_constraint("ck_dining_tables_extra_hour_fee", "dining_tables", "extra_hour_fee >= 0 AND extra_hour_fee <= 100000")
    op.add_column("reservations", sa.Column("base_fee_amount", sa.Numeric(10, 2), nullable=True))
    op.add_column("reservations", sa.Column("extra_hour_rate", sa.Numeric(10, 2), nullable=False, server_default="0.00"))
    op.add_column("reservations", sa.Column("extra_hours", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("reservations", sa.Column("extra_fee_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"))
    # Existing reservations keep their original price, regardless of their duration.
    op.execute("UPDATE reservations SET base_fee_amount = fee_amount")


def downgrade() -> None:
    for column in ("extra_fee_amount", "extra_hours", "extra_hour_rate", "base_fee_amount"):
        op.drop_column("reservations", column)
    op.drop_constraint("ck_dining_tables_extra_hour_fee", "dining_tables", type_="check")
    op.drop_column("dining_tables", "extra_hour_fee")
