"""add merchant to banking transaction

Revision ID: c1a2b3c4d5e6
Revises: b1c2d3e4f5a6
Create Date: 2025-08-04 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1a2b3c4d5e6'
down_revision = 'b1c2d3e4f5a6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('banking_transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('merchant', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('banking_transaction', schema=None) as batch_op:
        batch_op.drop_column('merchant')