"""Remove restaurant-level registration seating and table counts."""

from alembic import op
import sqlalchemy as sa


revision = "0050_remove_registration_counts"
down_revision = "583b4f90a780"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("restaurants", "capacity")
    op.drop_column("restaurants", "tables")


def downgrade() -> None:
    op.add_column("restaurants", sa.Column("capacity", sa.Integer(), nullable=True))
    op.add_column("restaurants", sa.Column("tables", sa.Integer(), nullable=True))
