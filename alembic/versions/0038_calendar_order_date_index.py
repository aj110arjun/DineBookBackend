"""Index manager calendar order date lookups."""
from alembic import op


revision = "0038_calendar_order_date_index"
down_revision = "0037_platform_commission_payouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_food_orders_restaurant_created",
        "food_orders",
        ["restaurant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_food_orders_restaurant_created", table_name="food_orders")
