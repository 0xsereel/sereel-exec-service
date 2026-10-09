"""x402 data feed: config, payments, NAV checkpoints

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel  # noqa: F401  (autogenerate emits sqlmodel types)


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'datafeed',
        sa.Column('strategy_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('price_usd', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('pay_to', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('fields', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('fund_address', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('updated_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('strategy_id'),
    )
    op.create_table(
        'datapayment',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('strategy_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('payer', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('amount_usd', sa.Numeric(precision=38, scale=12), nullable=False),
        sa.Column('mint', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('tx_signature', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('settled_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('fields_served', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tx_signature'),
    )
    op.create_index(op.f('ix_datapayment_strategy_id'), 'datapayment', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_datapayment_settled_at'), 'datapayment', ['settled_at'], unique=False)
    op.create_table(
        'navcheckpoint',
        sa.Column('id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('strategy_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('nav_per_share', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('unhedged_nav_per_share', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('as_of', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.Column('published_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('attestation_sig', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_navcheckpoint_strategy_id'), 'navcheckpoint', ['strategy_id'], unique=False)
    op.create_index(op.f('ix_navcheckpoint_as_of'), 'navcheckpoint', ['as_of'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_navcheckpoint_as_of'), table_name='navcheckpoint')
    op.drop_index(op.f('ix_navcheckpoint_strategy_id'), table_name='navcheckpoint')
    op.drop_table('navcheckpoint')
    op.drop_index(op.f('ix_datapayment_settled_at'), table_name='datapayment')
    op.drop_index(op.f('ix_datapayment_strategy_id'), table_name='datapayment')
    op.drop_table('datapayment')
    op.drop_table('datafeed')
