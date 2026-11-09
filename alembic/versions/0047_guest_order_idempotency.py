"""Enforce idempotency keys for guest orders."""
from alembic import op
import sqlalchemy as sa


revision = "0047_guest_order_idempotency"
down_revision = "e63184aba500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve historical orders: stop for manual reconciliation if keys were reused.
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE food_orders IN SHARE ROW EXCLUSIVE MODE")
    duplicate = connection.execute(sa.text(
        "SELECT idempotency_key FROM food_orders "
        "WHERE customer_id IS NULL AND idempotency_key IS NOT NULL "
        "GROUP BY idempotency_key HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    if duplicate is not None:
        raise RuntimeError(
            "Guest orders contain duplicate idempotency keys. Reconcile the orders "
            "before applying 0047_guest_order_idempotency; no orders were deleted."
        )
    op.create_index(
        "uq_food_orders_guest_idempotency_key", "food_orders", ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("customer_id IS NULL"),
        sqlite_where=sa.text("customer_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_food_orders_guest_idempotency_key", table_name="food_orders")
