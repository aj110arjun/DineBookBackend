"""Track different winning offers on separate items in one order."""
from alembic import op

revision = "0058_item_offer_redemptions"
down_revision = "0057_automatic_offers_coupons"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("offer_redemptions") as batch:
        batch.drop_constraint("uq_offer_redemptions_order", type_="unique")
        batch.create_unique_constraint("uq_offer_redemptions_order_offer", ["order_id", "offer_id"])


def downgrade():
    # Refuse to discard item-level history if multi-offer orders already exist.
    with op.batch_alter_table("offer_redemptions") as batch:
        batch.drop_constraint("uq_offer_redemptions_order_offer", type_="unique")
        batch.create_unique_constraint("uq_offer_redemptions_order", ["order_id"])
