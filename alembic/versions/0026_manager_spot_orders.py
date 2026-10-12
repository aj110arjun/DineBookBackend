"""Support manager-created spot orders for walk-in guests."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026_manager_spot_orders"
down_revision = "0025_reservation_order_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("food_orders", "customer_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column("food_orders", sa.Column("guest_name", sa.String(length=120), nullable=True))
    op.add_column("food_orders", sa.Column("table_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_food_orders_table_id_dining_tables", "food_orders", "dining_tables", ["table_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_food_orders_table_id_dining_tables", "food_orders", type_="foreignkey")
    op.drop_column("food_orders", "table_id")
    op.drop_column("food_orders", "guest_name")
    op.alter_column("food_orders", "customer_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
