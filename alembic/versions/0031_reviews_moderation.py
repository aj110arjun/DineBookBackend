"""Add restaurant reviews, flags, replies, and moderation audit."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0031_reviews_moderation"
down_revision = "0030_reservation_preorder_offers"
branch_labels = None
depends_on = None

def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("reviews",
        sa.Column("id", uuid, primary_key=True), sa.Column("customer_id", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("restaurant_id", uuid, sa.ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("food_id", uuid, sa.ForeignKey("food.id", ondelete="SET NULL")), sa.Column("order_id", uuid, sa.ForeignKey("food_orders.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False), sa.Column("content", sa.Text(), nullable=False), sa.Column("status", sa.String(20), nullable=False, server_default="PUBLISHED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("order_id", name="uq_reviews_order_id"), sa.UniqueConstraint("customer_id", "restaurant_id", name="uq_reviews_customer_restaurant"), sa.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_reviews_rating_range"))
    op.create_index("ix_reviews_customer_id", "reviews", ["customer_id"]); op.create_index("ix_reviews_restaurant_id", "reviews", ["restaurant_id"])
    op.create_index("ix_reviews_restaurant_status_created", "reviews", ["restaurant_id", "status", "created_at"])
    op.create_table("review_flags", sa.Column("id", uuid, primary_key=True), sa.Column("review_id", uuid, sa.ForeignKey("reviews.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False), sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("explanation", sa.String(1000)), sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"), sa.Column("resolution_note", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("resolved_at", sa.DateTime(timezone=True)))
    op.create_index("uq_review_flags_active_customer", "review_flags", ["review_id", "customer_id"], unique=True, postgresql_where=sa.text("status = 'PENDING'"))
    op.create_index("ix_review_flags_review_id", "review_flags", ["review_id"]); op.create_index("ix_review_flags_status_created", "review_flags", ["status", "created_at"])
    op.create_table("review_replies", sa.Column("id", uuid, primary_key=True), sa.Column("review_id", uuid, sa.ForeignKey("reviews.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("restaurant_id", uuid, sa.ForeignKey("restaurants.id", ondelete="RESTRICT"), nullable=False), sa.Column("manager_id", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("content", sa.String(2000), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("review_id", name="uq_review_replies_review_id"))
    op.create_table("review_moderation_events", sa.Column("id", uuid, primary_key=True), sa.Column("review_id", uuid, sa.ForeignKey("reviews.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("admin_id", uuid, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False), sa.Column("action", sa.String(20), nullable=False),
        sa.Column("previous_status", sa.String(20), nullable=False), sa.Column("new_status", sa.String(20), nullable=False), sa.Column("reason", sa.String(1000)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_review_moderation_events_review_created", "review_moderation_events", ["review_id", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_review_moderation_events_review_created", table_name="review_moderation_events"); op.drop_table("review_moderation_events")
    op.drop_table("review_replies"); op.drop_index("ix_review_flags_status_created", table_name="review_flags"); op.drop_index("ix_review_flags_review_id", table_name="review_flags"); op.drop_index("uq_review_flags_active_customer", table_name="review_flags"); op.drop_table("review_flags")
    op.drop_index("ix_reviews_restaurant_status_created", table_name="reviews"); op.drop_index("ix_reviews_restaurant_id", table_name="reviews"); op.drop_index("ix_reviews_customer_id", table_name="reviews"); op.drop_table("reviews")
