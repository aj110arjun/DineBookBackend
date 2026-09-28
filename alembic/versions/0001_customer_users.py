"""Create the shared DineBook users table.

Revision ID: 0001_customer_users
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_customer_users"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    user_role = sa.Enum("CUSTOMER", "CHEF", "MANAGER", "ADMIN", name="user_role")
    account_status = sa.Enum(
        "PENDING", "APPROVED", "REJECTED", "ACTIVE", "INACTIVE", "SUSPENDED",
        name="account_status",
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("status", account_status, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role_status", "users", ["role", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_users_role_status", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    sa.Enum(name="account_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)
