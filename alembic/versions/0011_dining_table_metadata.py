"""Add dining table type and audit timestamps where schema drift omitted them."""
from alembic import op
import sqlalchemy as sa

revision = "0011_dining_table_metadata"
down_revision = "0010_floor_table_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("dining_tables")}
    if "table_type" not in columns:
        op.add_column("dining_tables", sa.Column("table_type", sa.String(20), nullable=False, server_default="round"))
    if "created_at" not in columns:
        op.add_column("dining_tables", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    if "updated_at" not in columns:
        op.add_column("dining_tables", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("dining_tables")}
    for name in ("updated_at", "created_at", "table_type"):
        if name in columns:
            op.drop_column("dining_tables", name)
