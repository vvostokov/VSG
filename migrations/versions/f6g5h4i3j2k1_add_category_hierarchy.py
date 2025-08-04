"""add category hierarchy

Revision ID: f6g5h4i3j2k1
Revises: e1f2g3h4i5j6
Create Date: 2025-08-06 12:05:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6g5h4i3j2k1'
down_revision = 'e1f2g3h4i5j6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_id', sa.Integer(), nullable=True))
        batch_op.drop_constraint('_name_type_uc', type_='unique')
        batch_op.create_foreign_key('fk_category_parent_id', 'category', ['parent_id'], ['id'])
        batch_op.create_unique_constraint('_name_parent_type_uc', ['name', 'parent_id', 'type'])

def downgrade():
    with op.batch_alter_table('category', schema=None) as batch_op:
        batch_op.drop_constraint('_name_parent_type_uc', type_='unique')
        batch_op.drop_constraint('fk_category_parent_id', type_='foreignkey')
        batch_op.create_unique_constraint('_name_type_uc', ['name', 'type'])
        batch_op.drop_column('parent_id')