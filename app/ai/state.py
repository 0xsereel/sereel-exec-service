"""One structured snapshot of the market (and, optionally, one strategy) that every AI component reads.

Numbers are decimal strings. `to_state_text()` is a deterministic rendering (fixed section and key order, fixed formatting), so
the same input always gives the same text and the same `state_hash`. Nothing here sends an order or holds a key."""
import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from .. import pyth
from ..config import Market, settings
from . import extras
from . import hl_readonly as hl

D = Decimal
HOURS_PER_YEAR = 24 * 365


def fmt(x, places: int = 4) -> str:
    """Stable decimal string. None -> 'n/a'."""
    if x is None:
        return "n/a"
    q = D(1).scaleb(-places)
    s = format(D(str(x)).quantize(q), "f")
    return s


def pct(frac, places: int = 2) -> str:
    """A fraction rendered as an explicit percent ('0.2156' -> '21.56%'), so the unit can never be guessed."""
    return "n/a" if frac is None else fmt(D(str(frac)) * 100, places) + "%"


SECTION_LABELS = {"execution": "execution venue (not a market signal)", "sizing": "sizing"}


@dataclass
class Snapshot:
    market_id: str
    taken_at: str  # ISO 8601 UTC
    signals_network: str
    execution_network: str
    degraded: bool
    sections: dict[str, dict[str, str]] = field(default_factory=dict)
    absent: dict[str, str] = field(default_factory=dict)  # signal -> why it is missing (never silently dropped)
    has_strategy: bool = False
    excluded_markets: list[dict] = field(default_factory=list)  # for people only: NEVER rendered into the text Jev reads

    def to_state_text(self) -> str:
        lines = [f"market: {self.market_id}", f"as_of: {self.taken_at}", f"signals_network: {self.signals_network}",
                 f"execution_network: {self.execution_network}", f"degraded: {str(self.degraded).lower()}"]
        for name in ("price", "venue", "book", "volatility", "sizing", "execution", "calendar", "cross_venue", "strategy"):
            sec = self.sections.get(name)
            if sec:
                lines.append(f"[{SECTION_LABELS.get(name, name)}]")
                lines += [f"{k}: {v}" for k, v in sec.items()]  # insertion order is fixed by build_snapshot
        if self.absent:
            lines.append("[unavailable]")
            lines += [f"{k}: {v}" for k, v in sorted(self.absent.items())]
        return "\n".join(lines) + "\n"

    @property
    def state_hash(self) -> str:
        return hashlib.sha256(self.to_state_text().encode()).hexdigest()

    def get(self, section: str, key: str) -> D | None:
        v = self.sections.get(section, {}).get(key)
        try:
            return None if v in (None, "n/a") else D(v.rstrip("%"))  # a "%" value is returned as the percent number (21.56%% -> 21.56)
        except Exception:
            return None


def _log_returns(closes: list[D]) -> list[float]:
    return [math.log(float(b) / float(a)) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def realized_vol(closes: list[D]) -> D | None:
    """Annualized volatility (as a fraction) from hourly closes; None with fewer than 3 returns."""
    r = _log_returns(closes)
    if len(r) < 3:
        return None
    mean = sum(r) / len(r)
    var = sum((x - mean) ** 2 for x in r) / (len(r) - 1)
    return D(str(round(math.sqrt(var * HOURS_PER_YEAR), 6)))


def _pct_change(now: D, then: D | None) -> D | None:
    return None if not then else (now - then) / then * 100


def _depth(levels: list[dict], mid: D, is_ask: bool) -> D:
    lim = mid * (D("1.005") if is_ask else D("0.995"))
    return sum((D(l["sz"]) for l in levels if (D(l["px"]) <= lim if is_ask else D(l["px"]) >= lim)), D(0))


def _testnet_book(market: Market) -> dict | None:
    """Execution-side depth and mark from the testnet the orders would go to (info reads only)."""
    c = hl.asset_ctx(market.hl_coin, market.hl_dex, settings.hl_api_url)
    bids, asks = hl.l2_book(market.hl_coin, settings.hl_api_url)
    mark = D(c["markPx"])
    return {"mark": mark, "oracle": D(c["oraclePx"]), "buy_depth": _depth(asks, mark, True), "sell_depth": _depth(bids, mark, False)}


def build_snapshot(market: Market, strategy: dict | None = None, now: datetime | None = None, events: list[dict] | None = None,
                   size_oz: Decimal | None = None) -> Snapshot:
    """`strategy` is the serialized strategy (as GET /strategies/{id} returns it, with `value_usd` from /value merged in as
    `value_usd`), or None for a market-only snapshot. `size_oz` is a PROPOSED hedge size (setup), judged against testnet depth. Every read that can fail is isolated: it lands in `absent` with a reason."""
    now = now or datetime.now(timezone.utc)
    now_s = int(now.timestamp())
    network = settings.signals_source_network
    snap = Snapshot(market.market_id, now.strftime("%Y-%m-%dT%H:%M:%SZ"), network, "testnet" if settings.is_hl_testnet else "mainnet",
                    False, has_strategy=strategy is not None)
    absent = snap.absent

    # -- Pyth ------------------------------------------------------------------------------------------------------
    pyth_px = None
    try:
        q = pyth.get_price(market.pyth_feed_id, market.max_staleness_s, symbol=market.symbol)
        pyth_px = q.price
        snap.sections["price"] = {"pyth_price": fmt(q.price, 2), "pyth_confidence": fmt(q.conf, 4),
                                  "pyth_publish_time": datetime.fromtimestamp(q.publish_time, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                  "market_open": str(not q.market_closed).lower()}
    except Exception as e:
        absent["pyth_price"] = f"{getattr(e, 'code', type(e).__name__)}"

    # -- Hyperliquid (signals network), with a labelled testnet fallback ---------------------------------------------
    ctx = candles = book = None
    try:
        ctx = hl.asset_ctx(market.hl_coin, market.hl_dex)
        start = (now_s - 7 * 86400 - 3600) * 1000
        candles = hl.candles(market.hl_coin, "1h", start, now_s * 1000)
        book = hl.l2_book(market.hl_coin)
    except hl.SignalsReadError as e:
        absent["signals_reads"] = f"{network} reads failed ({e}); using the execution testnet, so signals are degraded"
        snap.degraded = True
        snap.signals_network = "testnet"
        try:
            ctx = ctx or hl.asset_ctx(market.hl_coin, market.hl_dex, settings.hl_api_url)
            book = book or hl.l2_book(market.hl_coin, settings.hl_api_url)
            candles = candles or hl.candles(market.hl_coin, "1h", (now_s - 7 * 86400 - 3600) * 1000, now_s * 1000, settings.hl_api_url)
        except hl.SignalsReadError as e2:
            absent["testnet_fallback"] = str(e2)

    closes = [D(c["c"]) for c in candles] if candles else []
    if ctx:
        mark = D(ctx["markPx"])
        funding = D(ctx["funding"])
        snap.sections["venue"] = {
            "hl_mark": fmt(mark, 2), "hl_oracle": fmt(ctx["oraclePx"], 2), "funding_rate_hourly": fmt(funding, 8),
            "funding_rate_annualized": pct(funding * HOURS_PER_YEAR), "open_interest_oz": fmt(ctx["openInterest"], 2),
            "mark_vs_pyth_bps": fmt((mark - pyth_px) / pyth_px * 10_000, 1) if pyth_px else "n/a"}
        if book and book[0] and book[1]:
            mid = (D(book[0][0]["px"]) + D(book[1][0]["px"])) / 2
            snap.sections["book"] = {
                "spread_bps": fmt((D(book[1][0]["px"]) - D(book[0][0]["px"])) / mid * 10_000, 2),
                "depth_buy_within_0.5pct_oz": fmt(_depth(book[1], mid, True), 4),
                "depth_sell_within_0.5pct_oz": fmt(_depth(book[0], mid, False), 4)}
        else:
            absent["book"] = "order book empty or unreadable"
    if closes:
        last = closes[-1]
        v24, v7 = realized_vol(closes[-25:]), realized_vol(closes[-169:])
        ch1 = _pct_change(last, closes[-2]) if len(closes) > 1 else None
        sigma_h = v24 / D(str(HOURS_PER_YEAR)).sqrt() * 100 if v24 else None  # one hour's 1-sigma move, in percent
        snap.sections["volatility"] = {
            "change_1h_pct": fmt(ch1, 3),
            "change_24h_pct": fmt(_pct_change(last, closes[-25]) if len(closes) > 24 else None, 3),
            "realized_vol_24h_annualized": pct(v24), "realized_vol_7d_annualized": pct(v7),
            "vol_ratio_24h_7d": fmt(v24 / v7, 2) if v24 and v7 else "n/a",
            "hourly_sigma_pct": fmt(sigma_h, 4),
            "move_1h_sigmas": fmt(abs(ch1) / sigma_h, 2) if ch1 is not None and sigma_h else "n/a"}
    else:
        absent["volatility"] = "no candles"

    # -- execution testnet depth/mark (what an order would actually meet) ---------------------------------------------
    try:
        t = _testnet_book(market)
        snap.sections["execution"] = {
            "testnet_mark": fmt(t["mark"], 2), "testnet_oracle": fmt(t["oracle"], 2),
            "testnet_mark_vs_pyth_bps": fmt((t["mark"] - pyth_px) / pyth_px * 10_000, 1) if pyth_px else "n/a",
            "testnet_depth_buy_within_0.5pct_oz": fmt(t["buy_depth"], 4), "testnet_depth_sell_within_0.5pct_oz": fmt(t["sell_depth"], 4)}
    except (hl.SignalsReadError, httpx.HTTPError, KeyError) as e:
        absent["execution_book"] = f"testnet reads failed ({type(e).__name__})"

    # -- optional extras: each failure is a note, never an error -------------------------------------------------------
    cal, why = extras.calendar(now, events)
    if cal:
        snap.sections["calendar"] = {"events_within_24h": "; ".join(cal["within_24h"]) or "none", "next_event": cal["next_event"],
                                     "source": cal["source"]}
    else:
        absent["calendar"] = why
    hist, why = extras.pyth_history(market.pyth_feed_id, now_s)
    if hist and pyth_px:
        snap.sections["price"].update({f"pyth_change_{k}_pct": fmt(_pct_change(pyth_px, v), 3) for k, v in hist.items()})
    elif not hist:
        absent["pyth_history"] = why
    absent["predicted_funding_xyz"] = extras.PREDICTED_REASON  # honest: predictedFundings does not list HIP-3 markets
    absent["paxg_jupiter"] = extras.PAXG_REASON  # dropped: no verified Solana mint, so no onchain gold cross-check
    cv, why = extras.cross_venue(market.hl_coin, pyth_px)
    if cv:
        sec = {}
        for v, rate in sorted(cv["paxg_funding_hourly"].items()):
            sec[f"paxg_perp_funding_hourly_{v}"] = fmt(rate, 8)
        for name, o in sorted(cv["included"].items()):
            sec[f"{name}_mark"], sec[f"{name}_funding_hourly"] = fmt(o["mark"], 2), fmt(o["funding"], 8)
        snap.excluded_markets = cv["excluded"]
        if sec:
            snap.sections["cross_venue"] = sec
        else:
            absent["cross_venue"] = "no cross-venue data included"
    else:
        absent["cross_venue"] = why

    # -- strategy -----------------------------------------------------------------------------------------------------
    if strategy:
        pos = strategy.get("position") or {}
        equity = D(str(strategy["value_usd"])) if strategy.get("value_usd") is not None else None
        maint = D(str(pos["maintenance_margin_usd"])) if pos.get("maintenance_margin_usd") else None
        sec = {
            "strategy_id": strategy["id"], "status": strategy["status"], "leverage": str(strategy["leverage"]),
            "exposure_oz": fmt(strategy["target_exposure_units"], 4), "hedge_ratio_pct": fmt(D(strategy["hedge_ratio_bps"]) / 100, 2),
            "target_hedge_oz": fmt(strategy["target_hedge_size_units"], 4), "hedge_oz": fmt(pos.get("size_units"), 4),
            "gap_oz": fmt(strategy.get("hedge_gap_units"), 4), "gap_pct": fmt(D(str(strategy.get("hedge_gap_bps") or 0)) / 100, 2),
            "rebalance_band_pct": fmt(D(strategy["rebalance_band_bps"]) / 100, 2), "margin_usd": fmt(pos.get("margin_usd"), 2),
            "equity_usd": fmt(equity, 2), "maintenance_margin_usd": fmt(maint, 4),
            "maintenance_ratio": fmt(equity / maint, 3) if equity is not None and maint else "n/a",
            "required_margin_usd": fmt(strategy.get("required_margin_usd"), 2),
            "equity_to_required_ratio": fmt(equity / D(str(strategy["required_margin_usd"])), 2)
            if equity is not None and strategy.get("required_margin_usd") else "n/a",
            "distance_to_liquidation_pct": "n/a", "unrealized_pnl_usd": fmt(pos.get("unrealized_pnl_usd"), 4),
            "funding_paid_usd": fmt(pos.get("funding_paid_usd"), 4)}
        liq, mk = pos.get("liquidation_price_usd"), pos.get("mark_price_usd")
        if liq and mk:
            sec["distance_to_liquidation_pct"] = fmt(abs(D(str(liq)) - D(str(mk))) / D(str(mk)) * 100, 2)
        snap.sections["strategy"] = sec

    _sizing(snap, strategy, size_oz)
    return snap


def _sizing(snap: Snapshot, strategy: dict | None, size_oz: D | None) -> None:
    """The size a liquidity question is about, the side it trades, and the TESTNET depth on that side (where orders execute).
    No known size -> no sizing section, and the liquidity question is reported absent instead of being guessed."""
    depth_sell = snap.get("execution", "testnet_depth_sell_within_0.5pct_oz")  # a sell hits the bids
    depth_buy = snap.get("execution", "testnet_depth_buy_within_0.5pct_oz")
    if strategy is not None:
        gap = D(str(strategy.get("hedge_gap_units") or 0))  # target short - current short: > 0 means sell more
        if gap == 0:
            snap.absent["liquidity_sufficient"] = "no rebalance needed (gap is zero), so no size to judge"
            return
        size, side, basis = abs(gap), "sell" if gap > 0 else "buy", "size of the rebalance needed"
    elif size_oz is not None and size_oz > 0:
        size, side, basis = size_oz, "sell", "proposed hedge (opening a short)"
    else:
        snap.absent["liquidity_sufficient_for_size"] = "no hedge size given"
        return
    depth = depth_sell if side == "sell" else depth_buy
    mark = snap.get("venue", "hl_mark") or snap.get("price", "pyth_price")
    snap.sections["sizing"] = {
        "size_basis": basis, "size_oz": fmt(size, 4), "side_to_trade": side,
        "size_notional_usd": fmt(size * mark, 2) if mark else "n/a",
        "testnet_depth_same_side_within_0.5pct_oz": fmt(depth, 4),
        "depth_to_size_ratio": fmt(depth / size, 2) if depth is not None else "n/a"}

