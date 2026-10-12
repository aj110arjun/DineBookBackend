"""Add image storage to food variants."""
from alembic import op
import sqlalchemy as sa


revision = "0022_food_variant_images"
down_revision = "0021_realtime_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("food_variants", sa.Column("image_url", sa.Text(), nullable=True))
    op.add_column("food_variants", sa.Column("image_public_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("food_variants", "image_public_id")
    op.drop_column("food_variants", "image_url")
