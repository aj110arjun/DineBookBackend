"""Allow restaurant-level reviews and manager moderation flags."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0045_review_flags"
down_revision = "0044_password_changed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_reviews_order_id", "reviews", type_="unique")
    op.alter_column("reviews", "order_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)

    op.alter_column("review_flags", "customer_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)
    op.add_column("review_flags", sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_review_flags_manager_id_users", "review_flags", "users", ["manager_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint(
        "ck_review_flags_single_reporter",
        "review_flags",
        "(customer_id IS NOT NULL AND manager_id IS NULL) OR (customer_id IS NULL AND manager_id IS NOT NULL)",
    )
    op.create_index(
        "uq_review_flags_active_manager",
        "review_flags",
        ["review_id", "manager_id"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_flags_active_manager", table_name="review_flags")
    op.drop_constraint("ck_review_flags_single_reporter", "review_flags", type_="check")
    op.drop_constraint("fk_review_flags_manager_id_users", "review_flags", type_="foreignkey")
    op.drop_column("review_flags", "manager_id")
    op.alter_column("review_flags", "customer_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.alter_column("reviews", "order_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
    op.create_unique_constraint("uq_reviews_order_id", "reviews", ["order_id"])
