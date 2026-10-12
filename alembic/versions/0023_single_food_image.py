"""Consolidate gallery and variant photos into one food image."""

from alembic import op
import sqlalchemy as sa


revision = "0023_single_food_image"
down_revision = "0022_food_variant_images"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the earliest explicitly uploaded food image (display order, then
    # creation time and ID make the choice stable between runs).
    op.execute(sa.text("""
        DELETE FROM food_images
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY food_item_id
                    ORDER BY display_order ASC, created_at ASC, id ASC
                ) AS image_rank
                FROM food_images
            ) ranked
            WHERE image_rank > 1
        )
    """))

    # For dishes without a food-level image, promote the earliest variant
    # image. Existing food-level images always take precedence.
    op.execute(sa.text("""
        INSERT INTO food_images (id, food_item_id, image_url, public_id, display_order, created_at)
        SELECT gen_random_uuid(), fallback.food_id, fallback.image_url,
               fallback.image_public_id, 0, CURRENT_TIMESTAMP
        FROM (
            SELECT DISTINCT ON (variant.food_id)
                variant.food_id, variant.image_url, variant.image_public_id
            FROM food_variants AS variant
            WHERE variant.image_url IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM food_images AS image
                  WHERE image.food_item_id = variant.food_id
              )
            ORDER BY variant.food_id, variant.created_at ASC, variant.id ASC
        ) AS fallback
    """))

    op.create_index("uq_food_images_food_item_id", "food_images", ["food_item_id"], unique=True)
    op.drop_index("ix_food_images_item_order", table_name="food_images")
    op.drop_column("food_variants", "image_public_id")
    op.drop_column("food_variants", "image_url")


def downgrade() -> None:
    op.add_column("food_variants", sa.Column("image_url", sa.Text(), nullable=True))
    op.add_column("food_variants", sa.Column("image_public_id", sa.String(length=255), nullable=True))
    op.drop_index("uq_food_images_food_item_id", table_name="food_images")
    op.create_index("ix_food_images_item_order", "food_images", ["food_item_id", "display_order"])
