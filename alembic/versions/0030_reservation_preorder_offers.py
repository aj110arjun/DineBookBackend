"""Persist offers selected for reservation preorders."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0030_reservation_preorder_offers"
down_revision = "0029_offer_applicability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reservations", sa.Column("preorder_discount", sa.Numeric(10, 2), nullable=False, server_default="0.00"))
    op.add_column("reservations", sa.Column("preorder_offer_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("reservations", sa.Column("preorder_offer_code", sa.String(40), nullable=True))
    op.create_foreign_key("fk_reservations_preorder_offer_id", "reservations", "food_order_offers", ["preorder_offer_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_reservations_preorder_offer_id", "reservations", type_="foreignkey")
    op.drop_column("reservations", "preorder_offer_code")
    op.drop_column("reservations", "preorder_offer_id")
    op.drop_column("reservations", "preorder_discount")
