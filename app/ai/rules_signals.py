"""The deterministic stand-in for Jev: the SAME questions with the SAME thresholds (see questions.py), computed on the snapshot's
numbers. Used when Jev is unconfigured or down; the result is labelled signals_source "rules". Values are 0.95 / 0.05 where the
threshold decides and 0.5 in the explicitly uncertain band, never pretending to more precision than that."""
from decimal import Decimal

from .state import Snapshot

D = Decimal
YES, NO, MAYBE = D("0.95"), D("0.05"), D("0.5")


def _band(value: D | None, yes: bool, no: bool) -> D:
    if value is None:
        return MAYBE
    return YES if yes else NO if no else MAYBE


def answer(snap: Snapshot, names: list[str]) -> dict[str, Decimal]:
    g = snap.get
    out: dict[str, Decimal] = {}
    for n in names:
        if n == "needs_top_up_soon":
            ratio, stress = g("strategy", "maintenance_ratio"), g("strategy", "stress_ratio_3sigma_1h")
            out[n] = MAYBE if ratio is None else YES if ratio < D("1.5") or (stress is not None and stress < D("1.5")) \
                else NO if ratio >= D("2.0") and (stress is None or stress >= D("1.5")) else MAYBE
        elif n == "should_rebalance":
            gap, band, usd = g("strategy", "gap_pct"), g("strategy", "rebalance_band_pct"), g("sizing", "size_notional_usd")
            out[n] = NO if gap is None or band is None or usd is None else YES if gap > band and usd >= D("10.5") else NO
        elif n == "abnormal_price_move":
            sig = g("volatility", "move_1h_sigmas")
            out[n] = _band(sig, sig is not None and sig > 3, sig is not None and sig < 2)
        elif n == "venue_price_divergence":
            bps = g("venue", "mark_vs_pyth_bps")
            a = abs(bps) if bps is not None else None
            out[n] = _band(a, a is not None and a > 15, a is not None and a < 8)
        elif n in ("liquidity_sufficient", "liquidity_sufficient_for_size"):
            r = g("sizing", "depth_to_size_ratio")
            out[n] = _band(r, r is not None and r >= 2, r is not None and r < 1)
        elif n == "excess_margin_safe_to_return":
            ratio, eq = g("strategy", "maintenance_ratio"), g("strategy", "equity_to_required_ratio")
            out[n] = NO if ratio is None or eq is None else YES if ratio > 3 and eq > 2 else NO
        elif n == "high_impact_event_soon":
            ev = snap.sections.get("calendar", {}).get("events_within_24h")
            out[n] = MAYBE if ev is None else NO if ev == "none" else YES
        elif n == "volatility_elevated":
            ratio, v24 = g("volatility", "vol_ratio_24h_7d"), g("volatility", "realized_vol_24h_annualized")
            out[n] = MAYBE if ratio is None and v24 is None else YES if (ratio or 0) > D("1.3") or (v24 or 0) > 30 else NO
        elif n == "funding_favors_shorts":
            f = g("venue", "funding_rate_annualized")
            out[n] = _band(f, f is not None and f > 2, f is not None and f < 1)
        else:
            raise KeyError(n)
    return out
