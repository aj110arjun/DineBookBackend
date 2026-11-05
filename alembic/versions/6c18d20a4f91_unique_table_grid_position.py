"""Prevent overlapping table positions on each floor."""
from alembic import op

revision = "6c18d20a4f91"
down_revision = "25ac76b8e2e2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_dining_tables_grid_position", "dining_tables",
        ["floor_id", "grid_row", "grid_col"],
    )


def downgrade():
    op.drop_constraint(
        "uq_dining_tables_grid_position", "dining_tables", type_="unique",
    )
