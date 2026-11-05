"""add refunded_at to platform commissions"""
from alembic import op
import sqlalchemy as sa


revision = '9cb707cecfcd'
down_revision = '0046_review_cooldown'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('platform_commissions', sa.Column('refunded_at', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column('platform_commissions', 'refunded_at')
