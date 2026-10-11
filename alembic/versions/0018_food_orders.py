"""Add food order checkout, price snapshots, and restaurant offers."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0018_food_orders"
down_revision = "0017_wallet_razorpay_topups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "food_order_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("minimum_subtotal", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("uses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_food_order_offers_restaurant_id", "food_order_offers", ["restaurant_id"])
    op.create_table(
        "food_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("food_order_offers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("offer_code", sa.String(40), nullable=True),
        sa.Column("items", postgresql.JSONB(), nullable=False),
        sa.Column("subtotal", sa.Numeric(10, 2), nullable=False),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default="0.00"),
        sa.Column("total", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="PLACED"),
        sa.Column("payment_method", sa.String(20), nullable=False, server_default="PAY_AT_DESK"),
        sa.Column("payment_status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("customer_note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_food_orders_customer_idempotency_key"),
    )
    op.create_index("ix_food_orders_restaurant_status_created", "food_orders", ["restaurant_id", "status", "created_at"])
    op.create_index("ix_food_orders_customer_created", "food_orders", ["customer_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_food_orders_customer_created", table_name="food_orders")
    op.drop_index("ix_food_orders_restaurant_status_created", table_name="food_orders")
    op.drop_table("food_orders")
    op.drop_index("ix_food_order_offers_restaurant_id", table_name="food_order_offers")
    op.drop_table("food_order_offers")
