"""Associate chef accounts with their owning manager."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_manager_owned_staff"
down_revision = "fa6b2dec4445"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_users_manager_id_users", "users", "users", ["manager_id"], ["id"], ondelete="CASCADE")

    op.create_index("ix_users_manager_id", "users", ["manager_id"])


def downgrade() -> None:
    op.drop_index("ix_users_manager_id", table_name="users")
    op.drop_constraint("fk_users_manager_id_users", "users", type_="foreignkey")
    op.drop_column("users", "manager_id")
