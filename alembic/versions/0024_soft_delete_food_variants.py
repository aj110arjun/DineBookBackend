"""Add soft-delete lifecycle to food variants."""

from alembic import op
import sqlalchemy as sa


revision = "0024_soft_delete_food_variants"
down_revision = "0023_single_food_image"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("food_variants", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("food_variants", "deleted_at")
