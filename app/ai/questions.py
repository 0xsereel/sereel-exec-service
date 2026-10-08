"""The fixed risk questions asked of Jev (type `noul`: the probability of yes, 0..1). Names are stable API; changing a question's
text or criteria means bumping QUESTION_SET_VERSION, which is recorded with every decision."""

QUESTION_SET_VERSION = "2026-10-08.1"


def _q(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


MONITORING = {
    "needs_top_up_soon": _q(
        "Will this hedge need more margin within the next hour to stay above 150% of maintenance margin (equity / maintenance)?",
        "The maintenance ratio is likely to fall below 1.5 within an hour",
        "The maintenance ratio is likely to stay above 1.5"),
    "should_rebalance": _q(
        "Is the hedge far enough from its target that rebalancing now is worth the trading fees?",
        "The gap to target is large relative to fees and the rebalance band",
        "The gap is small, inside the band, or not worth the fees"),
    "abnormal_price_move": _q(
        "Is the current gold price move abnormal compared with recent volatility?",
        "The recent move is far outside normal for the 24h and 7d volatility",
        "The recent move is within the normal range"),
    "venue_price_divergence": _q(
        "Is the Hyperliquid mark diverging from the Pyth price enough to make trading risky right now?",
        "The mark and Pyth disagree by enough that an order could fill at a bad price",
        "The mark and Pyth agree closely"),
    "liquidity_sufficient": _q(
        "Is there enough order book depth to trade the required size within 0.5% of the mark?",
        "Executable depth within 0.5% covers the size the rebalance needs",
        "Depth within 0.5% is smaller than the size needed"),
    "excess_margin_safe_to_return": _q(
        "Can excess margin be returned without risking margin health in the next day?",
        "Equity is well above 2x the requirement and the maintenance ratio would stay high after returning the excess",
        "Returning margin could push the maintenance ratio toward 1.5 within a day"),
    "high_impact_event_soon": _q(
        "Is a major scheduled economic event (FOMC decision or CPI release) due within the next 24 hours?",
        "A listed FOMC or CPI event falls within the next 24 hours",
        "No listed event falls within the next 24 hours"),
}

SETUP = {
    "volatility_elevated": _q(
        "Is gold volatility elevated compared with its 7-day norm?",
        "24h realized volatility is well above the 7d level",
        "24h realized volatility is at or below the 7d level"),
    "funding_favors_shorts": _q(
        "Is funding currently paying shorts and likely to keep doing so?",
        "Funding is positive (shorts receive) and stable across recent readings and venues",
        "Funding is negative, near zero, or flipping"),
    "liquidity_sufficient_for_size": _q(
        "Can a short of the proposed size be opened within 0.5% of the mark?",
        "Depth within 0.5% on the sell side covers the proposed size",
        "Depth within 0.5% on the sell side is smaller than the proposed size"),
}

# Questions that only make sense with a live strategy in the snapshot.
NEEDS_STRATEGY = {"needs_top_up_soon", "should_rebalance", "liquidity_sufficient", "excess_margin_safe_to_return"}


def question_names(with_strategy: bool) -> list[str]:
    names = [n for n in MONITORING if with_strategy or n not in NEEDS_STRATEGY]
    return names + ([] if with_strategy else list(SETUP))


def question_defs(names: list[str]) -> dict:
    allq = {**MONITORING, **SETUP}
    return {n: allq[n] for n in names}
