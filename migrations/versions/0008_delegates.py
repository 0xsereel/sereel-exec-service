"""delegates

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel types)


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'delegate',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('strategy_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('delegate_pubkey', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('allowed_actions', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('max_rebalance_oz_per_day', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('rebalance_within_band_only', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('expires_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('granted_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('granted_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('revoked_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=True),
        sa.Column('attestation_sig', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('revoke_attestation_sig', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_delegate_strategy_id'), 'delegate', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_delegate_delegate_pubkey'), 'delegate', ['delegate_pubkey'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_delegate_delegate_pubkey'), table_name='delegate')
    op.drop_index(op.f('ix_delegate_strategy_id'), table_name='delegate')
    op.drop_table('delegate')
