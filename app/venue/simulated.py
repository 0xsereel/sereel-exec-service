"""Offline venue: Pyth (or mock) prices, in-memory shared account. Same interface as the real venue."""
import itertools
from decimal import Decimal

from .. import pyth
from ..config import Market
from ..errors import ServiceError
from .base import PositionState, VenueAdapter

TAKER_FEE = Decimal("0.00045")


class SimulatedVenue(VenueAdapter):
    name = "simulated"
    account_key = "simulated"

    def __init__(self, markets: dict[str, Market], fill_fraction: Decimal = Decimal(1),
                 funds: Decimal = Decimal(1_000_000)):
        super().__init__(markets)
        self.fill_fraction = fill_fraction  # < 1 simulates partial IOC fills
        self.funds = funds  # total USDC available to move onto the dex
        self._pos: dict[str, Decimal] = {}
        self._entry: dict[str, Decimal] = {}
        self._cash: dict[str, Decimal] = {}  # dex balance excluding unrealized P&L
        self._fees: dict[str, Decimal] = {}
        self._oid = itertools.count(1)
        self.price_override: dict[str, Decimal] = {}
        self._lev: dict[str, dict] = {}
        self.orders: list[dict] = []  # every order sent, for tests
        self.liquidity: Decimal | None = None  # tests: size offered on either side; None = unlimited
        self.funding_log: list[tuple[int, Decimal]] = []  # tests append (time_ms, signed usd) here

    def size_decimals(self, market_id):
        return 4

    def mark_price(self, market_id):
        if market_id in self.price_override:
            return self.price_override[market_id]
        return pyth.get_price(self.market(market_id).pyth_feed_id, None).price

    def available_liquidity(self, market_id, is_buy, limit_px):
        return Decimal("Infinity") if self.liquidity is None else self.liquidity

    def funding_entries(self, market_id, since_ms):
        return [(ms, usd) for ms, usd in self.funding_log if ms > since_ms]

    def leverage_status(self, market_id):
        return self._lev.get(market_id, {"leverage": 20, "mode": "cross"})  # the venue default before we set it

    def prepare_market(self, market_id):
        self._lev[market_id] = {"leverage": self.market(market_id).max_leverage, "mode": "isolated"}

    def position(self, strategy_id, market_id):
        mark = self.mark_price(market_id)
        size = self._pos.get(market_id, Decimal(0))
        upnl = (mark - self._entry.get(market_id, Decimal(0))) * size if size else Decimal(0)
        return PositionState(size=size, entry_px=self._entry.get(market_id, Decimal(0)), mark=mark, unrealized_pnl=upnl,
                             account_value=self._cash.get(market_id, Decimal(0)) + upnl, margin_used=abs(size) * mark / 3)

    def ensure_margin(self, market_id, usd_amount):
        cur = self._cash.get(market_id, Decimal(0))
        if usd_amount <= cur:
            return
        need = usd_amount - cur
        if need > self.funds:
            raise ServiceError("INSUFFICIENT_MARGIN", f"need {need} more on the dex but only {self.funds} is available")
        self.funds -= need
        self._cash[market_id] = usd_amount

    def release_margin(self, market_id: str, usd_amount: Decimal) -> None:
        """Move USDC from the dex balance back to the main balance (withdrawal path)."""
        self._cash[market_id] = self._cash.get(market_id, Decimal(0)) - usd_amount
        self.funds += usd_amount

    def _ioc(self, market_id, is_buy, size, limit_px, reduce_only=False):
        self.orders.append({"market": market_id, "is_buy": is_buy, "size": size, "reduce_only": reduce_only})
        mark = self.mark_price(market_id)
        if (is_buy and limit_px < mark) or (not is_buy and limit_px > mark):
            return None, None  # limit not marketable: IOC cancels with zero fill
        fill = (size * self.fill_fraction).quantize(Decimal(1).scaleb(-self.size_decimals(market_id)))
        if fill == 0:
            return None, None
        signed = fill if is_buy else -fill
        old = self._pos.get(market_id, Decimal(0))
        entry = self._entry.get(market_id, mark)
        closing = min(abs(signed), abs(old)) if old and (old > 0) != (signed > 0) else Decimal(0)
        if closing:  # realize P&L on the part that reduces the position
            self._cash[market_id] = self._cash.get(market_id, Decimal(0)) + (mark - entry) * closing * (1 if old > 0 else -1)
        opening = abs(signed) - closing
        new = old + signed
        if new == 0:
            self._entry[market_id] = Decimal(0)
        elif opening and closing:  # flipped through zero: remainder opens at mark
            self._entry[market_id] = mark
        elif opening:  # adding in the same direction (or from flat): weighted average
            self._entry[market_id] = (entry * abs(old) + mark * opening) / (abs(old) + opening) if old else mark
        self._pos[market_id] = new
        oid = str(next(self._oid))
        fee = fill * mark * TAKER_FEE
        self._fees[oid] = fee
        self._cash[market_id] = self._cash.get(market_id, Decimal(0)) - fee
        return mark, oid

    def _fees_for(self, market_id, oids, fallback_notional):
        return sum((self._fees.get(o, Decimal(0)) for o in oids), Decimal(0))
