"""Persist reservation and food-order lifecycle milestones."""

from alembic import op
import sqlalchemy as sa


revision = "0025_reservation_order_lifecycle"
down_revision = "0024_soft_delete_food_variants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reservations", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("reservations", sa.Column("status_reason", sa.String(length=500), nullable=True))
    op.add_column("food_orders", sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("food_orders", sa.Column("preparation_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("food_orders", sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("food_orders", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("food_orders", sa.Column("status_reason", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("food_orders", "status_reason")
    op.drop_column("food_orders", "completed_at")
    op.drop_column("food_orders", "ready_at")
    op.drop_column("food_orders", "preparation_started_at")
    op.drop_column("food_orders", "accepted_at")
    op.drop_column("reservations", "status_reason")
    op.drop_column("reservations", "completed_at")
    op.drop_column("reservations", "checked_in_at")
