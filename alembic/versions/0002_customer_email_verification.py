"""Add customer email verification.

Revision ID: 0002_customer_email_verification
Revises: 0001_customer_users
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_customer_email_verification"
down_revision = "0001_customer_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "email_verification_codes",
        sa.Column("email", sa.String(length=320), primary_key=True, nullable=False),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("email_verification_codes")
    op.drop_column("users", "email_verified")
