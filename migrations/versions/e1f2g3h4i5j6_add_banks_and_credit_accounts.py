"""add banks and credit accounts

Revision ID: e1f2g3h4i5j6
Revises: d6e5f4g3h2i1
Create Date: 2025-08-06 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e1f2g3h4i5j6'
down_revision = 'd6e5f4g3h2i1'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('bank',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.add_column(sa.Column('credit_limit', sa.Numeric(precision=20, scale=2), nullable=True))
        batch_op.add_column(sa.Column('grace_period_days', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('bank_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_account_bank_id', 'bank', ['bank_id'], ['id'])

def downgrade():
    with op.batch_alter_table('account', schema=None) as batch_op:
        batch_op.drop_constraint('fk_account_bank_id', type_='foreignkey')
        batch_op.drop_column('bank_id')
        batch_op.drop_column('grace_period_days')
        batch_op.drop_column('credit_limit')
    op.drop_table('bank')