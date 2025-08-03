"""add to_amount to banking_transaction

Revision ID: d6e5f4g3h2i1
Revises: c1a2b3c4d5e6
Create Date: 2025-08-05 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd6e5f4g3h2i1'
down_revision = 'c1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('banking_transaction', schema=None) as batch_op:
        batch_op.add_column(sa.Column('to_amount', sa.Numeric(precision=20, scale=2), nullable=True))


def downgrade():
    with op.batch_alter_table('banking_transaction', schema=None) as batch_op:
        batch_op.drop_column('to_amount')