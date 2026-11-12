"""Add indexes for customer restaurant listing queries."""
from alembic import op


revision = "0040_restaurant_listing_indexes"
down_revision = "0039_order_tax_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_restaurants_status_created_at",
        "restaurants",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_restaurant_documents_restaurant_type_created",
        "restaurant_documents",
        ["restaurant_id", "document_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_restaurant_documents_restaurant_type_created",
        table_name="restaurant_documents",
    )
    op.drop_index("ix_restaurants_status_created_at", table_name="restaurants")
