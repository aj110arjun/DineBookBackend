"""Scope offer code uniqueness to each restaurant."""

from alembic import op
import sqlalchemy as sa


revision = "0051_restaurant_offer_codes"
down_revision = "0050_remove_registration_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Older installs may have a generated or manually named global constraint.
    for constraint in sa.inspect(op.get_bind()).get_unique_constraints("food_order_offers"):
        if constraint["column_names"] == ["code"]:
            op.drop_constraint(constraint["name"], "food_order_offers", type_="unique")
    op.create_index(
        "uq_food_order_offers_restaurant_code", "food_order_offers",
        ["restaurant_id", sa.text("upper(code)")], unique=True,
    )


def downgrade() -> None:
    # This fails safely if different restaurants have since reused a code.
    op.create_unique_constraint("food_order_offers_code_key", "food_order_offers", ["code"])
    op.drop_index("uq_food_order_offers_restaurant_code", table_name="food_order_offers")
