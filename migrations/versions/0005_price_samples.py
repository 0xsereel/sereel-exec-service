"""price samples

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel types)


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pricesample',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('market_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('price', sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column('ts', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_pricesample_market_id'), 'pricesample', ['market_id'], unique=False)
    op.create_index(op.f('ix_pricesample_source'), 'pricesample', ['source'], unique=False)
    op.create_index(op.f('ix_pricesample_ts'), 'pricesample', ['ts'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_pricesample_ts'), table_name='pricesample')
    op.drop_index(op.f('ix_pricesample_source'), table_name='pricesample')
    op.drop_index(op.f('ix_pricesample_market_id'), table_name='pricesample')
    op.drop_table('pricesample')
