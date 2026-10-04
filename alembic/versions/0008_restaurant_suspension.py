"""Add suspended restaurant state."""
from alembic import op

revision = "0008_restaurant_suspension"
down_revision = "0007_soft_delete_menu"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE restaurant_status ADD VALUE IF NOT EXISTS 'SUSPENDED'")


def downgrade() -> None:
    # PostgreSQL does not support removing an enum value safely.
    pass
