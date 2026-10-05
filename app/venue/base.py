"""Venue abstraction.

All strategies share one venue account (and one xyz:GOLD position). One lock per venue account serialises
"order + position re-read", so a fill is attributed to a strategy as the position delta measured inside that lock.
Per-strategy ledger math lives in strategies/service.py.
"""
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal

from .. import pyth
from ..config import Market, settings
from ..errors import ServiceError

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def account_lock(key: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


@dataclass
class PositionState:
    size: Decimal = Decimal(0)  # signed, account level
    entry_px: Decimal = Decimal(0)
    mark: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)
    liquidation_px: Decimal | None = None
    account_value: Decimal = Decimal(0)  # dex margin balance incl. unrealized P&L
    margin_used: Decimal = Decimal(0)


@dataclass
class Partial:
    attempt: int
    limit_px: Decimal
    filled: Decimal  # signed, measured from the position, not from the order response
    avg_px: Decimal | None
    oid: str | None


@dataclass
class FillReport:
    market_id: str
    requested: Decimal  # signed delta asked for
    filled: Decimal = Decimal(0)  # signed delta actually filled (position delta inside the lock)
    remaining: Decimal = Decimal(0)  # signed gap still open
    avg_px: Decimal | None = None
    fee: Decimal = Decimal(0)
    oids: list[str] = field(default_factory=list)
    partials: list[Partial] = field(default_factory=list)
    position_after: PositionState | None = None


class VenueAdapter(ABC):
    name = "venue"
    account_key = "default"

    def __init__(self, markets: dict[str, Market]):
        self.markets = markets

    def market(self, market_id: str) -> Market:
        try:
            return self.markets[market_id]
        except KeyError:
            raise ServiceError("UNKNOWN_MARKET", f"unknown market {market_id}", 404)

    # -- venue specifics ------------------------------------------------------
    @abstractmethod
    def mark_price(self, market_id: str) -> Decimal: ...

    @abstractmethod
    def position(self, strategy_id: str | None, market_id: str) -> PositionState:
        """Account-level position (strategy_id is for interface parity; the ledger is per strategy)."""

    @abstractmethod
    def ensure_margin(self, market_id: str, usd_amount: Decimal) -> None:
        """Make sure the market's margin balance holds at least usd_amount, else raise INSUFFICIENT_MARGIN."""

    @abstractmethod
    def size_decimals(self, market_id: str) -> int: ...

    @abstractmethod
    def _ioc(self, market_id: str, is_buy: bool, size: Decimal, limit_px: Decimal) -> tuple[Decimal | None, str | None]:
        """Send one IOC order; return (reported avg px, oid). The caller measures the real fill from the position."""

    @abstractmethod
    def _fees_for(self, market_id: str, oids: list[str], fallback_notional: Decimal) -> Decimal: ...

    def release_margin(self, market_id: str, usd_amount: Decimal) -> None:
        """Move USDC from the market's dex balance back to the main balance (withdrawal path)."""
        raise NotImplementedError

    def funding_since(self, market_id: str, since_ms: int) -> Decimal:
        return Decimal(0)

    def margin_balance(self, market_id: str) -> Decimal:
        return self.position(None, market_id).account_value

    # -- shared logic ---------------------------------------------------------
    def check_price(self, market_id: str) -> Decimal:
        """Reject on stale Pyth or venue-mark/Pyth deviation beyond MAX_PRICE_DEVIATION_BPS."""
        m = self.market(market_id)
        mark = self.mark_price(market_id)
        ref = pyth.get_price(m.pyth_feed_id, m.max_staleness_s).price
        dev_bps = abs(mark - ref) / ref * 10_000
        if dev_bps > settings.max_price_deviation_bps:
            raise ServiceError("PRICE_DEVIATION", f"{self.name} mark {mark} vs Pyth {ref}: {dev_bps:.0f}bps")
        return mark

    def set_position(self, strategy_id: str, market_id: str, target_signed_size: Decimal,
                     current_size: Decimal = Decimal(0), slippage: Decimal = Decimal("0.005")) -> FillReport:
        """Move this strategy's size from current_size to target by trading the delta on the shared account.

        Up to IOC_MAX_RETRIES IOC orders at mark +/- slippage. Every attempt is recorded; the final report carries
        the remaining gap. Raises ORDER_NOT_FILLED only when nothing filled at all.
        """
        delta = Decimal(target_signed_size) - Decimal(current_size)
        q = Decimal(1).scaleb(-self.size_decimals(market_id))
        report = FillReport(market_id=market_id, requested=delta, remaining=delta)
        with account_lock(self.account_key):
            mark = self.check_price(market_id)
            if abs(delta) < q:
                report.position_after = self.position(strategy_id, market_id)
                report.remaining = Decimal(0)
                return report
            goal = self.position(strategy_id, market_id).size + delta
            for attempt in range(1, settings.ioc_max_retries + 1):
                before = self.position(strategy_id, market_id).size
                gap = goal - before
                size = abs(gap).quantize(q, rounding=ROUND_DOWN)
                if size == 0:
                    break
                is_buy = gap > 0
                mark = self.mark_price(market_id)
                limit = mark * (1 + slippage if is_buy else 1 - slippage)
                avg_px, oid = self._ioc(market_id, is_buy, size, limit)
                got = self.position(strategy_id, market_id).size - before
                report.partials.append(Partial(attempt, limit, got, avg_px, oid))
                if oid:
                    report.oids.append(oid)
                if got:
                    prev, px = abs(report.filled), avg_px or mark
                    report.avg_px = px if not report.avg_px else (report.avg_px * prev + px * abs(got)) / (prev + abs(got))
                report.filled += got
            report.position_after = self.position(strategy_id, market_id)
            report.remaining = goal - report.position_after.size
            if abs(report.remaining) < q:
                report.remaining = Decimal(0)
            report.fee = self._fees_for(market_id, report.oids, abs(report.filled) * (report.avg_px or mark))
        if report.filled == 0:
            raise ServiceError("ORDER_NOT_FILLED", f"no fill after {settings.ioc_max_retries} IOC attempts on {market_id}")
        return report


class FundingRoute(ABC):
    """Moves stablecoin between Solana and the venue margin account."""

    @abstractmethod
    def to_venue(self, amount_usd: Decimal, ref: str) -> dict: ...

    @abstractmethod
    def from_venue(self, amount_usd: Decimal, to_solana: str, ref: str) -> dict: ...


class MirroredRoute(FundingRoute):
    """Testnet: Solana deposits are mirrored; pre-funded venue USDC is the margin. Nothing moves."""

    def to_venue(self, amount_usd, ref):
        return {"route": "mirrored", "amount": str(amount_usd), "ref": ref}

    def from_venue(self, amount_usd, to_solana, ref):
        return {"route": "mirrored", "amount": str(amount_usd), "ref": ref}


class CctpHyperliquidRoute(FundingRoute):
    """Production stub: Solana USDC -> CCTP -> Arbitrum -> Hyperliquid bridge -> xyz dex (and reverse)."""

    def to_venue(self, amount_usd, ref):
        raise NotImplementedError("CCTP + Hyperliquid bridge route is not implemented")

    def from_venue(self, amount_usd, to_solana, ref):
        raise NotImplementedError("Hyperliquid withdraw -> CCTP -> Solana route is not implemented")
