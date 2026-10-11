"""Link food orders to their reservation dining session."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0020_reservation_orders"
down_revision = "0019_preorder_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "food_orders",
        sa.Column(
            "reservation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reservations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index("ix_food_orders_reservation_id", "food_orders", ["reservation_id"])


def downgrade() -> None:
    op.drop_index("ix_food_orders_reservation_id", table_name="food_orders")
    op.drop_column("food_orders", "reservation_id")
