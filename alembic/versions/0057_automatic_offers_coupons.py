"""Distinguish automatic menu offers from coupon codes."""
from alembic import op
import sqlalchemy as sa

revision = "0057_automatic_offers_coupons"
down_revision = "0056_floor_zones"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("food_order_offers") as batch:
        batch.add_column(sa.Column("application_mode", sa.String(16), nullable=False, server_default="AUTOMATIC"))
        batch.create_check_constraint("ck_food_order_offer_application_mode", "application_mode IN ('AUTOMATIC', 'COUPON')")


def downgrade():
    with op.batch_alter_table("food_order_offers") as batch:
        batch.drop_constraint("ck_food_order_offer_application_mode", type_="check")
        batch.drop_column("application_mode")
