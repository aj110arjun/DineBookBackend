"""Record manager no-show time for customer reservation cooldown."""
from alembic import op
import sqlalchemy as sa


revision = "0059_no_show_cooldown"
down_revision = "85172d83670f"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "reservations",
        sa.Column("no_show_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_reservations_user_no_show_at",
        "reservations",
        ["user_id", "no_show_at"],
    )


def downgrade():
    op.drop_index("ix_reservations_user_no_show_at", table_name="reservations")
    op.drop_column("reservations", "no_show_at")
