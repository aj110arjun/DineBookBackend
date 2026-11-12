"""Add name-only restaurant staff roster entries."""
from alembic import op
import sqlalchemy as sa


revision = "0042_restaurant_staff_roster"
down_revision = "0041_restaurant_coordinates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restaurant_staff",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("restaurant_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["restaurant_id"], ["restaurants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_restaurant_staff_restaurant_created",
        "restaurant_staff",
        ["restaurant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_restaurant_staff_restaurant_created", table_name="restaurant_staff")
    op.drop_table("restaurant_staff")
