"""Optional named zones within restaurant floors."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0056_floor_zones"
down_revision = "0055_customer_ai_support"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("floor_zones",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("floor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurant_floors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.UniqueConstraint("id", "floor_id", name="uq_floor_zones_id_floor"))
    op.create_index("ix_floor_zones_floor_id", "floor_zones", ["floor_id"])
    op.create_index("uq_floor_zones_name", "floor_zones", ["floor_id", sa.text("lower(name)")], unique=True)
    with op.batch_alter_table("dining_tables") as batch:
        batch.add_column(sa.Column("zone_id", postgresql.UUID(as_uuid=True), nullable=True))
        batch.create_index("ix_dining_tables_zone_id", ["zone_id"])
        batch.create_foreign_key("fk_dining_tables_zone_floor", "floor_zones", ["zone_id", "floor_id"], ["id", "floor_id"], ondelete="RESTRICT")


def downgrade():
    with op.batch_alter_table("dining_tables") as batch:
        batch.drop_constraint("fk_dining_tables_zone_floor", type_="foreignkey")
        batch.drop_index("ix_dining_tables_zone_id")
        batch.drop_column("zone_id")
    op.drop_index("uq_floor_zones_name", table_name="floor_zones")
    op.drop_index("ix_floor_zones_floor_id", table_name="floor_zones")
    op.drop_table("floor_zones")
