"""Tables. Statuses follow the Cantina v4 contract; money is Decimal in the DB (JSON numbers at the API edge)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, Column, Numeric, UniqueConstraint
from sqlmodel import Field, SQLModel

# Strategy.status
S_PENDING, S_ACTIVE, S_REBALANCING, S_CLOSING = "pending_funding", "active", "rebalancing", "closing"
S_CLOSED, S_EXPIRED, S_CANCELLED, S_FAILED = "closed", "expired", "cancelled", "failed"
# StrategyDeposit.status
D_PENDING, D_CONFIRMED, D_EXPIRED, D_CANCELLED = "pending_funding", "confirmed", "expired", "cancelled"
# Withdrawal.status (failed is reachable from any of the first four)
W_REQUESTED, W_POSITION_CLOSED, W_RELEASED, W_BRIDGING = "requested", "position_closed", "released", "bridging"
W_COMPLETED, W_FAILED = "completed", "failed"


def now() -> datetime:
    return datetime.now(timezone.utc)  # always tz-aware UTC; serialised as ISO 8601 at the edge


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4()}"


def money(default: str = "0"):
    return Field(default=Decimal(default), sa_column=Column(Numeric(38, 12), nullable=False, default=Decimal(default)))


def nullable_money():
    return Field(default=None, sa_column=Column(Numeric(38, 12), nullable=True))


class Strategy(SQLModel, table=True):
    """A hedge. Also the deploy intent: intent_id is the memo the funding transfer must carry."""
    id: str = Field(default_factory=new_id, primary_key=True)
    template: str = "delta_neutral_hedge"
    status: str = Field(default=S_PENDING, index=True)
    fund_id: str = Field(index=True)
    fund_name: str = ""
    market_id: str
    market_symbol: str = ""
    hedge_ratio_bps: int
    leverage: int
    rebalance_band_bps: int
    target_exposure_units: Decimal = money()
    return_wallet_address: str
    owner_user_id: str = Field(default="", index=True)
    org_id: str = Field(default="", index=True)
    # who may manage this strategy (signed-message authorization): exactly one is set, bound at creation
    owner_pubkey: str | None = None  # the manager's Sereel Solana wallet
    owner_multisig: str | None = None  # a Squads v4 multisig ACCOUNT whose current members may manage it
    failure_reason: str | None = None

    # deploy intent
    intent_id: str = Field(default_factory=new_id, unique=True, index=True)
    registered_sender_address: str = Field(index=True)
    multisig: bool = False
    expected_amount_usd: Decimal = money()
    required_margin_usd: Decimal = money()  # notional / leverage * (1 + buffer), frozen when the strategy was created
    received_amount_usd: Decimal | None = nullable_money()
    expires_at: datetime
    deploy_signature: str | None = None  # inbound transfer that completed the funding
    activation_attempts: int = 0  # funded, but the first order has not gone through yet (informational)
    funded_at: datetime | None = None  # when funding completed: the deploy retry window is measured from here, in time not attempts

    # per-strategy ledger on the shared venue account
    margin_usd: Decimal = money()  # credited margin (deposits + excess)
    size: Decimal = money()  # signed hedge size in units (negative = short)
    entry_px: Decimal = money()
    realized_pnl_usd: Decimal = money()
    fees_usd: Decimal = money()
    funding_usd: Decimal = money()
    funding_cursor_ms: int = 0  # last funding-history timestamp already booked
    hl_order_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    last_attestation_sig: str | None = None

    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    deployed_at: datetime | None = None
    closed_at: datetime | None = None


class StrategyDeposit(SQLModel, table=True):
    """Top-up intent; on match it credits margin to the strategy."""
    id: str = Field(default_factory=new_id, primary_key=True)
    strategy_id: str = Field(index=True)
    intent_id: str = Field(default_factory=new_id, unique=True, index=True)
    amount_usd: Decimal = money()
    expected_amount_usd: Decimal = money()
    received_amount_usd: Decimal | None = nullable_money()
    source_wallet_address: str | None = None  # actual sender, once seen
    registered_sender_address: str = Field(index=True)
    multisig: bool = False
    status: str = Field(default=D_PENDING, index=True)
    expires_at: datetime
    solana_signature: str | None = None
    created_at: datetime = Field(default_factory=now)


class ChainTransfer(SQLModel, table=True):
    """Every inbound stablecoin transfer seen on the funding address. Primary key = signature, so none is processed twice."""
    signature: str = Field(primary_key=True)
    sender: str | None = None
    amount_usd: Decimal = money()
    memo: str | None = None
    intent_id: str | None = None
    disposition: str  # processing | credited | refunded | ignored_own | refund_failed | refund_unconfirmed
    note: str | None = None  # why it was refunded / why a refund failed
    refund_signature: str | None = None
    attestation_sig: str | None = None
    created_at: datetime = Field(default_factory=now)


class WatcherCursor(SQLModel, table=True):
    name: str = Field(primary_key=True)
    last_signature: str | None = None  # newest finalized signature fully processed
    updated_at: datetime = Field(default_factory=now)


class Action(SQLModel, table=True):
    """Activity history. `record` is the full record whose sha256 the Solana memo commits to."""
    id: str = Field(default_factory=new_id, primary_key=True)
    strategy_id: str = Field(index=True)
    action: str  # deploy | deposit | rebalance | edit_hedge_settings | return_excess | close | snapshot | refund ...
    signer_public_key: str | None = None
    authorization: dict | None = Field(default=None, sa_column=Column(JSON))
    record: dict = Field(default_factory=dict, sa_column=Column(JSON))
    hl_order_ids: list = Field(default_factory=list, sa_column=Column(JSON))
    solana_signature: str | None = None  # related transfer, if any
    attestation_sig: str | None = None
    created_at: datetime = Field(default_factory=now, index=True)


class PnlSnapshot(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    strategy_id: str = Field(index=True)
    ts: datetime = Field(default_factory=now, index=True)
    unrealized_pnl_usd: Decimal = money()
    realized_pnl_usd: Decimal = money()
    funding_usd: Decimal = money()
    fees_usd: Decimal = money()
    hedge_pnl_usd: Decimal = money()  # unrealized + realized + funding - fees; margin never included
    margin_usd: Decimal = money()
    cause: str = "tick"  # tick | action name


class Withdrawal(SQLModel, table=True):
    """Return-excess and close share this state machine."""
    id: str = Field(default_factory=new_id, primary_key=True)
    strategy_id: str = Field(index=True)
    type: str = "return_excess"  # return_excess | close
    amount_usd: Decimal = money()
    destination_wallet_address: str
    status: str = Field(default=W_REQUESTED, index=True)
    failure_reason: str | None = None
    solana_signature: str | None = None
    attestation_sig: str | None = None
    auth_nonce: str = Field(unique=True, index=True)  # idempotency key: a retried request returns the same row
    refs: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)


class UsedNonce(SQLModel, table=True):
    """Replay protection for signed-message authorizations; rows older than the TTL are pruned."""
    nonce: str = Field(primary_key=True)
    created_at: datetime = Field(default_factory=now, index=True)


class Schedule(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    name: str = Field(index=True)
    fund_id: str | None = None
    to: str
    interval_seconds: int
    amount_mode: str = "fixed"  # fixed | priced
    amount_usd: Decimal = money()
    pricing: dict | None = Field(default=None, sa_column=Column(JSON))
    memo_template: str = "Revenue payment {seq}"
    max_payments: int | None = None
    end_at: datetime | None = None
    status: str = "active"  # active | paused | stopped | done
    seq: int = 0  # last claimed payment number
    total_paid_usd: Decimal = money()
    next_run: datetime | None = None
    created_at: datetime = Field(default_factory=now)


class Payment(SQLModel, table=True):
    # (schedule_id, seq) is unique: a scheduled run can only ever be claimed once, whatever process asks
    __table_args__ = (UniqueConstraint("schedule_id", "seq"),)
    id: str = Field(default_factory=new_id, primary_key=True)
    schedule_id: str | None = Field(default=None, index=True)
    seq: int | None = None
    to: str = Field(index=True)
    amount_usd: Decimal = money()
    memo: str = ""
    signature: str | None = None
    status: str = "claimed"  # claimed -> sent | failed | unconfirmed (claimed is written BEFORE sending; never auto-resent)
    error: str | None = None
    created_at: datetime = Field(default_factory=now)
