"""The deterministic stand-in for Jev: the same questions answered from thresholds on the snapshot. Used when Jev is not
configured or fails; the result is labelled signals_source "rules" so nobody mistakes it for a model's calibrated number.
Probabilities here are coarse on purpose (0.05 / 0.6 / 0.9...), never pretending to precision."""
from decimal import Decimal

from ..config import settings
from .state import Snapshot

D = Decimal


def _p(x: str) -> Decimal:
    return D(x)


def answer(snap: Snapshot, names: list[str]) -> dict[str, Decimal]:
    g = snap.get
    out: dict[str, Decimal] = {}
    ratio = g("strategy", "maintenance_ratio")
    equity, required = g("strategy", "equity_usd"), g("strategy", "required_margin_usd")
    gap_pct, band_pct = g("strategy", "gap_pct"), g("strategy", "rebalance_band_pct")
    v24, v7 = g("volatility", "realized_vol_24h_annualized"), g("volatility", "realized_vol_7d_annualized")
    ch1 = g("volatility", "change_1h_pct")
    for n in names:
        if n == "needs_top_up_soon":
            out[n] = _p("0.05") if ratio is None else _p("0.95") if ratio < D("1.5") else _p("0.6") if ratio < D("1.8") else _p("0.05")
        elif n == "should_rebalance":
            out[n] = _p("0.05") if gap_pct is None or band_pct is None else _p("0.9") if gap_pct > band_pct else _p("0.1")
        elif n == "abnormal_price_move":
            if ch1 is None or not v24:
                out[n] = _p("0.1")
            else:
                hourly_sigma_pct = v24 / D(str(24 * 365)).sqrt() * 100  # annualized -> one hour, in percent
                z = abs(ch1) / hourly_sigma_pct if hourly_sigma_pct else D(0)
                out[n] = _p("0.9") if z > 3 else _p("0.6") if z > 2 else _p("0.05")
        elif n == "venue_price_divergence":
            bps = g("execution", "testnet_mark_vs_pyth_bps")
            bps = abs(bps) if bps is not None else None
            lim = settings.max_price_deviation_bps
            out[n] = _p("0.1") if bps is None else _p("0.95") if bps >= lim else _p("0.6") if bps >= lim / 2 else _p("0.05")
        elif n == "liquidity_sufficient":
            have = g("execution", "testnet_depth_buy_within_0.5pct_oz")
            need = g("strategy", "gap_oz")
            out[n] = _p("0.5") if have is None or need is None else _p("0.95") if have >= abs(need) else _p("0.1")
        elif n == "excess_margin_safe_to_return":
            out[n] = _p("0.9") if ratio is not None and ratio > 3 and equity and required and equity > 2 * required else _p("0.1")
        elif n == "high_impact_event_soon":
            ev = snap.sections.get("calendar", {}).get("events_within_24h")
            out[n] = _p("0.1") if ev is None else _p("0.02") if ev == "none" else _p("0.95")
        elif n == "volatility_elevated":
            out[n] = _p("0.1") if not v24 or not v7 else _p("0.8") if v24 > v7 * D("1.3") else _p("0.2")
        elif n == "funding_favors_shorts":
            f = g("venue", "funding_rate_hourly")
            out[n] = _p("0.1") if f is None else _p("0.8") if f > 0 else _p("0.1")
        elif n == "liquidity_sufficient_for_size":
            have = g("execution", "testnet_depth_sell_within_0.5pct_oz")
            out[n] = _p("0.5") if have is None else _p("0.9") if have >= D("0.05") else _p("0.1")
        else:
            raise KeyError(n)
    return out
