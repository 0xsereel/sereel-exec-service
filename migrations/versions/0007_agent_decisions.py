"""agent decisions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel types)


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'agentdecision',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('strategy_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('state_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('signals_source', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('signals_network', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('question_set_version', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('signals', sa.JSON(), nullable=False),
        sa.Column('decision', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('action', sa.JSON(), nullable=True),
        sa.Column('explanation', sa.JSON(), nullable=False),
        sa.Column('reason', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('downgraded_from', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('outcome', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('action_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('attestation_sig', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_agentdecision_strategy_id'), 'agentdecision', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_agentdecision_at'), 'agentdecision', ['at'], unique=False)
    op.create_index(op.f('ix_agentdecision_outcome'), 'agentdecision', ['outcome'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_agentdecision_outcome'), table_name='agentdecision')
    op.drop_index(op.f('ix_agentdecision_at'), table_name='agentdecision')
    op.drop_index(op.f('ix_agentdecision_strategy_id'), table_name='agentdecision')
    op.drop_table('agentdecision')
