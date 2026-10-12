"""Complete floor and dining table management fields."""
from alembic import op
import sqlalchemy as sa

revision = "0010_floor_table_management"
down_revision = "0009_floors_and_dining_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("floors", "restaurant_floors")
    op.execute("ALTER INDEX ix_floors_restaurant_id RENAME TO ix_restaurant_floors_restaurant_id")
    op.add_column("restaurant_floors", sa.Column("floor_number", sa.Integer(), nullable=True))
    op.add_column("restaurant_floors", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("restaurant_floors", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("restaurant_floors", sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("restaurant_floors", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("""
        WITH numbered AS (
          SELECT id, row_number() OVER (PARTITION BY restaurant_id ORDER BY name, id) AS n
          FROM restaurant_floors
        )
        UPDATE restaurant_floors SET floor_number = numbered.n
        FROM numbered WHERE restaurant_floors.id = numbered.id
    """)
    op.execute("UPDATE restaurant_floors SET created_at = now(), updated_at = now() WHERE created_at IS NULL")
    op.alter_column("restaurant_floors", "name", new_column_name="floor_name")
    op.alter_column("restaurant_floors", "floor_number", nullable=False)
    op.alter_column("restaurant_floors", "created_at", nullable=False)
    op.alter_column("restaurant_floors", "updated_at", nullable=False)
    op.drop_constraint("uq_floors_restaurant_name", "restaurant_floors", type_="unique")
    op.create_unique_constraint("uq_floors_restaurant_number", "restaurant_floors", ["restaurant_id", "floor_number"])

    op.drop_constraint("uq_dining_tables_restaurant_floor_number", "dining_tables", type_="unique")
    op.drop_constraint("fk_dining_tables_floor_restaurant", "dining_tables", type_="foreignkey")
    op.drop_constraint("dining_tables_floor_id_fkey", "dining_tables", type_="foreignkey")
    op.alter_column("dining_tables", "table_number", type_=sa.String(30), postgresql_using="table_number::varchar")
    op.alter_column("dining_tables", "seats", new_column_name="capacity")
    op.create_unique_constraint("uq_dining_tables_floor_number", "dining_tables", ["floor_id", "table_number"])
    op.create_foreign_key("fk_dining_tables_floor_restaurant", "dining_tables", "restaurant_floors", ["floor_id", "restaurant_id"], ["id", "restaurant_id"], ondelete="RESTRICT")


def downgrade() -> None:
    op.drop_constraint("fk_dining_tables_floor_restaurant", "dining_tables", type_="foreignkey")
    op.drop_constraint("uq_dining_tables_floor_number", "dining_tables", type_="unique")
    op.alter_column("dining_tables", "capacity", new_column_name="seats")
    op.alter_column("dining_tables", "table_number", type_=sa.Integer(), postgresql_using="table_number::integer")
    op.create_unique_constraint("uq_dining_tables_restaurant_floor_number", "dining_tables", ["restaurant_id", "floor_id", "table_number"])
    op.create_foreign_key("dining_tables_floor_id_fkey", "dining_tables", "restaurant_floors", ["floor_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("fk_dining_tables_floor_restaurant", "dining_tables", "restaurant_floors", ["floor_id", "restaurant_id"], ["id", "restaurant_id"], ondelete="CASCADE")
    op.drop_constraint("uq_floors_restaurant_number", "restaurant_floors", type_="unique")
    op.alter_column("restaurant_floors", "floor_name", new_column_name="name")
    op.create_unique_constraint("uq_floors_restaurant_name", "restaurant_floors", ["restaurant_id", "name"])
    op.drop_column("restaurant_floors", "updated_at")
    op.drop_column("restaurant_floors", "created_at")
    op.drop_column("restaurant_floors", "is_active")
    op.drop_column("restaurant_floors", "description")
    op.drop_column("restaurant_floors", "floor_number")
    op.rename_table("restaurant_floors", "floors")
    op.execute("ALTER INDEX ix_restaurant_floors_restaurant_id RENAME TO ix_floors_restaurant_id")
