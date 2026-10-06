"""Add manager-controlled reservation fee to dining tables."""
from alembic import op
import sqlalchemy as sa

revision = "0013_table_reservation_fee"
down_revision = "0012_menu_image_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dining_tables", sa.Column("reservation_fee", sa.Numeric(10, 2), nullable=False, server_default="250"))


def downgrade() -> None:
    op.drop_column("dining_tables", "reservation_fee")
