"""Add optional customer contact and managed avatar references to existing users."""

from alembic import op
import sqlalchemy as sa


revision = "0052_customer_profile"
down_revision = "0051_restaurant_offer_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(20), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(2048), nullable=True))
    op.add_column("users", sa.Column("avatar_public_id", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "avatar_public_id")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "phone")
