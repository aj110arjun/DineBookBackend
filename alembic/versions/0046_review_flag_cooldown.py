"""Allow a customer to review again after a moderated removal cooldown."""
from alembic import op
import sqlalchemy as sa


revision = "0046_review_cooldown"
down_revision = "0045_review_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_reviews_customer_restaurant", "reviews", type_="unique")
    op.create_index(
        "uq_reviews_customer_restaurant_active",
        "reviews",
        ["customer_id", "restaurant_id"],
        unique=True,
        postgresql_where=sa.text("status != 'REMOVED'"),
    )


def downgrade() -> None:
    op.drop_index("uq_reviews_customer_restaurant_active", table_name="reviews")
    op.create_unique_constraint("uq_reviews_customer_restaurant", "reviews", ["customer_id", "restaurant_id"])
