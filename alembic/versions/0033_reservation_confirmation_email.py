"""Track reservation confirmation email delivery."""
from alembic import op
import sqlalchemy as sa

revision = "0033_reservation_email"
down_revision = "0032_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("confirmation_email_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("reservations", "confirmation_email_sent_at")
