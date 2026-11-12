"""Extend food order offers with applicability and redemption history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
import uuid

revision = "0029_offer_applicability"
down_revision = "0028_reservation_razorpay"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("food_order_offers", "discount_percent", existing_type=sa.Numeric(5, 2), type_=sa.Numeric(10, 2), existing_nullable=False)
    op.add_column("food_order_offers", sa.Column("title", sa.String(120), nullable=False, server_default="Offer"))
    op.add_column("food_order_offers", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("food_order_offers", sa.Column("discount_type", sa.String(16), nullable=False, server_default="PERCENTAGE"))
    op.add_column("food_order_offers", sa.Column("max_discount", sa.Numeric(10, 2), nullable=True))
    op.add_column("food_order_offers", sa.Column("per_customer_limit", sa.Integer(), nullable=True))
    op.add_column("food_order_offers", sa.Column("applicability", sa.String(16), nullable=False, server_default="RESTAURANT"))
    op.add_column("food_order_offers", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_check_constraint("ck_food_order_offer_discount_positive", "food_order_offers", "discount_percent > 0")
    op.create_check_constraint("ck_food_order_offer_minimum_nonnegative", "food_order_offers", "minimum_subtotal >= 0")
    op.create_check_constraint("ck_food_order_offer_uses_within_limit", "food_order_offers", "uses >= 0 AND (max_uses IS NULL OR (max_uses > 0 AND uses <= max_uses))")
    op.create_check_constraint("ck_food_order_offer_discount_type", "food_order_offers", "discount_type IN ('PERCENTAGE', 'FIXED')")
    op.create_check_constraint("ck_food_order_offer_applicability", "food_order_offers", "applicability IN ('RESTAURANT', 'ITEMS', 'CATEGORIES')")
    op.create_table("offer_eligible_foods",
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("food_order_offers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("food_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("food.id", ondelete="CASCADE"), primary_key=True))
    op.create_table("offer_eligible_categories",
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("food_order_offers.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True))
    op.create_table(
        "offer_redemptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("offer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("food_order_offers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("food_orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="APPLIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("order_id", name="uq_offer_redemptions_order"),
    )
    op.create_index("ix_offer_redemptions_offer_customer", "offer_redemptions", ["offer_id", "customer_id"])
    # Preserve the history of coupon-backed orders created before this ledger.
    bind = op.get_bind()
    legacy = bind.execute(sa.text("SELECT id, offer_id, customer_id, discount_amount, created_at, status FROM food_orders WHERE offer_id IS NOT NULL"))
    for order_id, offer_id, customer_id, amount, created_at, order_status in legacy:
        bind.execute(sa.text("INSERT INTO offer_redemptions (id, offer_id, order_id, customer_id, discount_amount, status, created_at) VALUES (:id, :offer_id, :order_id, :customer_id, :amount, :status, :created_at)"), {
            "id": uuid.uuid4(), "offer_id": offer_id, "order_id": order_id, "customer_id": customer_id,
            "amount": amount, "status": "REVERSED" if order_status in ("CANCELLED", "REJECTED") else "APPLIED", "created_at": created_at,
        })
    bind.execute(sa.text("UPDATE food_order_offers SET uses = (SELECT count(*) FROM offer_redemptions r WHERE r.offer_id = food_order_offers.id AND r.status = 'APPLIED')"))


def downgrade() -> None:
    op.drop_index("ix_offer_redemptions_offer_customer", table_name="offer_redemptions")
    op.drop_table("offer_redemptions")
    for name in ("ck_food_order_offer_discount_positive", "ck_food_order_offer_minimum_nonnegative", "ck_food_order_offer_uses_within_limit", "ck_food_order_offer_discount_type", "ck_food_order_offer_applicability"):
        op.drop_constraint(name, "food_order_offers", type_="check")
    op.drop_table("offer_eligible_categories")
    op.drop_table("offer_eligible_foods")
    for name in ("updated_at", "applicability", "per_customer_limit", "max_discount", "discount_type", "description", "title"):
        op.drop_column("food_order_offers", name)
    op.alter_column("food_order_offers", "discount_percent", existing_type=sa.Numeric(10, 2), type_=sa.Numeric(5, 2), existing_nullable=False)
