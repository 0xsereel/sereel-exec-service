"""chat sessions

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel types)


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'chatsession',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('owner_pubkey', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('strategy_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('updated_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('expires_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('user_messages', sa.Integer(), nullable=False),
        sa.Column('messages', sa.JSON(), nullable=False),
        sa.Column('slots', sa.JSON(), nullable=False),
        sa.Column('context', sa.JSON(), nullable=False),
        sa.Column('recommendation', sa.JSON(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chatsession_owner_pubkey'), 'chatsession', ['owner_pubkey'], unique=False)
    op.create_index(op.f('ix_chatsession_strategy_id'), 'chatsession', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_chatsession_expires_at'), 'chatsession', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_chatsession_expires_at'), table_name='chatsession')
    op.drop_index(op.f('ix_chatsession_strategy_id'), table_name='chatsession')
    op.drop_index(op.f('ix_chatsession_owner_pubkey'), table_name='chatsession')
    op.drop_table('chatsession')
