"""Require manager-created chefs to change their temporary password."""

from alembic import op
import sqlalchemy as sa

revision = "0004_chef_password_change"
down_revision = "0003_manager_owned_staff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "must_change_password")
