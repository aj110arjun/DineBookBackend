"""Add restaurant floors and dining tables."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_floors_and_dining_tables"
down_revision = "0008_restaurant_suspension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "floors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.UniqueConstraint("restaurant_id", "name", name="uq_floors_restaurant_name"),
        sa.UniqueConstraint("id", "restaurant_id", name="uq_floors_id_restaurant"),
    )
    op.create_index("ix_floors_restaurant_id", "floors", ["restaurant_id"])
    op.create_table(
        "dining_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("floor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("floors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_number", sa.Integer(), nullable=False),
        sa.Column("seats", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("status", sa.String(30), nullable=False, server_default="available"),
        sa.Column("table_type", sa.String(20), nullable=False, server_default="round"),
        sa.UniqueConstraint("restaurant_id", "floor_id", "table_number", name="uq_dining_tables_restaurant_floor_number"),
        sa.ForeignKeyConstraint(["floor_id", "restaurant_id"], ["floors.id", "floors.restaurant_id"], ondelete="CASCADE", name="fk_dining_tables_floor_restaurant"),
    )
    op.create_index("ix_dining_tables_floor_id", "dining_tables", ["floor_id"])


def downgrade() -> None:
    op.drop_table("dining_tables")
    op.drop_table("floors")
