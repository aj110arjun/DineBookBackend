"""Add reservations and reservation to table assignments."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0014_reservations"
down_revision = "0013_table_reservation_fee"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("restaurant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("reservation_date", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("number_of_guests", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING_PAYMENT"),
        sa.Column("special_request", sa.Text()),
        sa.Column("fee_amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("payment_status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_reservations_restaurant_date_status", "reservations", ["restaurant_id", "reservation_date", "status"])
    op.create_index("ix_reservations_user_date", "reservations", ["user_id", "reservation_date"])
    op.create_table(
        "reservation_tables",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("reservations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("table_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("dining_tables.id", ondelete="RESTRICT"), nullable=False),
    )
    op.create_index("ix_reservation_tables_table", "reservation_tables", ["table_id"])


def downgrade() -> None:
    op.drop_index("ix_reservation_tables_table", table_name="reservation_tables")
    op.drop_table("reservation_tables")
    op.drop_index("ix_reservations_user_date", table_name="reservations")
    op.drop_index("ix_reservations_restaurant_date_status", table_name="reservations")
    op.drop_table("reservations")
