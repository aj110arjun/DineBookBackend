"""Remove restaurant latitude and longitude for now."""

from alembic import op
import sqlalchemy as sa


revision = "0005_drop_restaurant_coords"
down_revision = "0004_chef_password_change"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("restaurants", "longitude")
    op.drop_column("restaurants", "latitude")


def downgrade() -> None:
    op.add_column("restaurants", sa.Column("latitude", sa.Numeric(10, 7), nullable=True))
    op.add_column("restaurants", sa.Column("longitude", sa.Numeric(10, 7), nullable=True))
