"""Index scoped dashboard settlement, refund and customer-history aggregation."""
from alembic import op

revision = "0054_manager_dashboard_indexes"
down_revision = "0053_customer_settings"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_commissions_restaurant_occurred", "platform_commissions", ["restaurant_id", "occurred_at"]),
    ("ix_commissions_restaurant_refunded", "platform_commissions", ["restaurant_id", "refunded_at"]),
    ("ix_food_orders_restaurant_customer_created", "food_orders", ["restaurant_id", "customer_id", "created_at"]),
    ("ix_reservations_restaurant_user_date", "reservations", ["restaurant_id", "user_id", "reservation_date"]),
)


def upgrade():
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
