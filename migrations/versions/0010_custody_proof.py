"""custody proof on NAV checkpoints

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel types)


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('navcheckpoint', schema=None) as batch_op:
        batch_op.add_column(sa.Column('custody_proof', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('navcheckpoint', schema=None) as batch_op:
        batch_op.drop_column('custody_proof')
