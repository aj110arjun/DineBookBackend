"""Support multiple service periods and date-specific operating exceptions."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0027_operating_hours_exceptions"
down_revision = "0026_manager_spot_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("restaurant_hours", sa.Column("periods", sa.JSON(), nullable=True))
    op.add_column("restaurant_hours", sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_restaurant_hours_updated_by_users", "restaurant_hours", "users", ["updated_by"], ["id"], ondelete="SET NULL")
    op.create_table(
        "restaurant_hours_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("is_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("periods", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("internal_note", sa.String(length=1000), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["restaurant_id"], ["restaurants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("end_date >= start_date", name="ck_restaurant_hours_exception_date_range"),
    )
    op.create_index("ix_restaurant_hours_exceptions_restaurant_id", "restaurant_hours_exceptions", ["restaurant_id"])
    op.create_index("ix_restaurant_hours_exceptions_start_date", "restaurant_hours_exceptions", ["start_date"])


def downgrade() -> None:
    op.drop_index("ix_restaurant_hours_exceptions_start_date", table_name="restaurant_hours_exceptions")
    op.drop_index("ix_restaurant_hours_exceptions_restaurant_id", table_name="restaurant_hours_exceptions")
    op.drop_table("restaurant_hours_exceptions")
    op.drop_constraint("fk_restaurant_hours_updated_by_users", "restaurant_hours", type_="foreignkey")
    op.drop_column("restaurant_hours", "updated_by")
    op.drop_column("restaurant_hours", "periods")
