"""Add soft-delete timestamps for menu categories and food."""

from alembic import op
import sqlalchemy as sa

revision = "0007_soft_delete_menu"
down_revision = "0006_normalized_menu"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("categories", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("food", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("uq_categories_restaurant_name", "categories", type_="unique")
    op.create_index(
        "uq_categories_restaurant_name_active",
        "categories",
        ["restaurant_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_categories_restaurant_name_active", table_name="categories")
    op.drop_column("food", "deleted_at")
    op.drop_column("categories", "deleted_at")
    op.create_unique_constraint("uq_categories_restaurant_name", "categories", ["restaurant_id", "name"])
