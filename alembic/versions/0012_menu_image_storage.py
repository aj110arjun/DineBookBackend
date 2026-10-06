"""Store provider identifiers for managed menu image deletion."""
from alembic import op
import sqlalchemy as sa


revision = "0012_menu_image_storage"
down_revision = "0011_dining_table_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("food_images", sa.Column("public_id", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("food_images", "public_id")
