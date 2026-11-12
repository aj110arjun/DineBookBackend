"""Invalidate sessions issued before a password change."""
from alembic import op
import sqlalchemy as sa

revision = "0044_password_changed_at"
down_revision = "0043_rate_limit_buckets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")
