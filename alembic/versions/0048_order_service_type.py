"""Normalize dine-in orders and enforce takeaway table rules."""

from alembic import op
import sqlalchemy as sa


revision = "0048_order_service_type"
down_revision = "f84f1834a9c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "food_orders", "service_type",
        existing_type=sa.String(10), existing_nullable=False,
        server_default="DINE_IN",
    )
    op.execute("UPDATE food_orders SET service_type = 'DINE_IN' WHERE service_type = 'DINEIN'")
    op.create_check_constraint(
        "ck_food_orders_service_type", "food_orders",
        "service_type IN ('DINE_IN', 'TAKEAWAY')",
    )
    op.create_check_constraint(
        "ck_food_orders_takeaway_no_visit", "food_orders",
        "service_type != 'TAKEAWAY' OR (table_id IS NULL AND reservation_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_food_orders_takeaway_no_visit", "food_orders", type_="check")
    op.drop_constraint("ck_food_orders_service_type", "food_orders", type_="check")
    op.execute("UPDATE food_orders SET service_type = 'DINEIN' WHERE service_type = 'DINE_IN'")
    op.alter_column(
        "food_orders", "service_type",
        existing_type=sa.String(10), existing_nullable=False,
        server_default="DINEIN",
    )
