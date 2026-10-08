"""The fixed risk questions asked of Jev (type `noul`: the probability of yes, 0..1).

Every question names the exact state fields it reads and a numeric threshold, in BOTH `instructions` and `criteria`, so the model
compares numbers instead of forming an impression (an earlier set let it answer 0.83 on a 1.1x volatility ratio). The same
thresholds are mirrored in rules_signals.py. Changing any text means bumping QUESTION_SET_VERSION, which is recorded with every
decision."""

QUESTION_SET_VERSION = "2026-10-08.2"


def _q(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


MONITORING = {
    "needs_top_up_soon": _q(
        "Will this hedge need more margin within the next hour to stay above a maintenance_ratio of 1.5? Read maintenance_ratio in "
        "[strategy] and move_1h_sigmas in [volatility]. Answer yes if maintenance_ratio is below 1.5, or if it is below 1.8 and "
        "move_1h_sigmas is above 2. Answer no if maintenance_ratio is at least 2.0.",
        "maintenance_ratio < 1.5, or maintenance_ratio < 1.8 with move_1h_sigmas > 2",
        "maintenance_ratio >= 2.0, or between 1.8 and 2.0 with move_1h_sigmas <= 2"),
    "should_rebalance": _q(
        "Is rebalancing now worth the trading fees? Read gap_pct and rebalance_band_pct in [strategy] and size_notional_usd in "
        "[sizing]. Answer yes only if gap_pct is above rebalance_band_pct AND size_notional_usd is at least 10.50. If there is no "
        "[sizing] section the gap is zero: answer no.",
        "gap_pct > rebalance_band_pct and size_notional_usd >= 10.50",
        "gap_pct <= rebalance_band_pct, or size_notional_usd < 10.50, or no [sizing] section"),
    "abnormal_price_move": _q(
        "Is the current gold move abnormal for recent volatility? Read move_1h_sigmas in [volatility]: the last hour's move as a "
        "multiple of one hour's normal 1-sigma move. Answer yes if move_1h_sigmas is above 3. Answer no if it is below 2.",
        "move_1h_sigmas > 3",
        "move_1h_sigmas < 2 (between 2 and 3 is uncertain)"),
    "venue_price_divergence": _q(
        "Is the Hyperliquid mark diverging from Pyth enough to make trading risky? Read ONLY mark_vs_pyth_bps in [venue] (the "
        "mainnet mark against Pyth, in basis points). Ignore [execution venue (not a market signal)], [cross_venue] and every "
        "other section. Answer yes if the absolute value of mark_vs_pyth_bps is above 15; no if it is below 8.",
        "abs(mark_vs_pyth_bps) > 15",
        "abs(mark_vs_pyth_bps) < 8 (between 8 and 15 is uncertain)"),
    "liquidity_sufficient": _q(
        "Is there enough order book depth to make the rebalance within 0.5% of the mark? Read size_oz, side_to_trade and "
        "depth_to_size_ratio in [sizing]; depth is the TESTNET depth on that side, because orders execute on testnet. Answer yes "
        "if depth_to_size_ratio is at least 2; no if it is below 1.",
        "depth_to_size_ratio >= 2",
        "depth_to_size_ratio < 1 (between 1 and 2 is uncertain)"),
    "excess_margin_safe_to_return": _q(
        "Can excess margin be returned without risking margin health for the next day? Read maintenance_ratio and "
        "equity_to_required_ratio in [strategy]. Answer yes if maintenance_ratio is above 3.0 AND equity_to_required_ratio is above "
        "2.0. Answer no if either is below its threshold.",
        "maintenance_ratio > 3.0 and equity_to_required_ratio > 2.0",
        "maintenance_ratio <= 3.0 or equity_to_required_ratio <= 2.0"),
    "high_impact_event_soon": _q(
        "Is a major scheduled economic event (FOMC decision or CPI release) due within the next 24 hours? Read "
        "events_within_24h in [calendar]. Answer yes if it names an event; no if it says none.",
        "events_within_24h names an event",
        "events_within_24h is none"),
}

SETUP = {
    "volatility_elevated": _q(
        "Is gold volatility elevated? Read vol_ratio_24h_7d and realized_vol_24h_annualized in [volatility]. Answer yes if "
        "vol_ratio_24h_7d is above 1.3 OR realized_vol_24h_annualized is above 30%. Answer no if vol_ratio_24h_7d is at most 1.3 "
        "AND realized_vol_24h_annualized is at most 30%.",
        "vol_ratio_24h_7d > 1.3 or realized_vol_24h_annualized > 30%",
        "vol_ratio_24h_7d <= 1.3 and realized_vol_24h_annualized <= 30%"),
    "funding_favors_shorts": _q(
        "Is funding paying shorts? Positive funding means longs pay shorts. Read funding_rate_annualized in [venue] (a percent). "
        "Answer yes if it is above +2%; no if it is below +1%.",
        "funding_rate_annualized > +2%",
        "funding_rate_annualized < +1% (between 1% and 2% is uncertain)"),
    "liquidity_sufficient_for_size": _q(
        "Can the proposed short be opened within 0.5% of the mark? Read size_oz, side_to_trade and depth_to_size_ratio in [sizing]; "
        "depth is the TESTNET depth on that side, because orders execute on testnet. Answer yes if depth_to_size_ratio is at least "
        "2; no if it is below 1.",
        "depth_to_size_ratio >= 2",
        "depth_to_size_ratio < 1 (between 1 and 2 is uncertain)"),
}

# Questions that only make sense with a live strategy in the snapshot.
NEEDS_STRATEGY = {"needs_top_up_soon", "should_rebalance", "liquidity_sufficient", "excess_margin_safe_to_return"}
# Questions that need a size in [sizing]: without one they are NOT asked and the snapshot says why (never a guessed number).
NEEDS_SIZING = {"liquidity_sufficient", "liquidity_sufficient_for_size"}


def question_names(snap) -> list[str]:
    """The questions that can be answered from this snapshot. A question missing its inputs is left out, and build_snapshot has
    already recorded why under [unavailable]."""
    names = [n for n in MONITORING if snap.has_strategy or n not in NEEDS_STRATEGY]
    if not snap.has_strategy:
        names += list(SETUP)
    return [n for n in names if n not in NEEDS_SIZING or "sizing" in snap.sections]


def question_defs(names: list[str]) -> dict:
    allq = {**MONITORING, **SETUP}
    return {n: allq[n] for n in names}
