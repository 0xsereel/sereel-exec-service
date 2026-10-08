"""Deterministic action parameters for an existing strategy: what a top-up, a return of excess margin or a rebalance would be.
The numbers are computed here from the strategy's own state, never by a model. Shared by the chat (drafts) and the monitoring loop
(proposals). Every value is a decimal string."""
import math
from decimal import Decimal

from ..config import settings
from ..strategies import service

D = Decimal
TARGET_RATIO = D(2)  # a top-up brings equity / maintenance back to 2.0
EXCESS_FLOOR = D("1.5")  # returning excess leaves 1.5x the required margin (the withdrawal rule)


def strategy_view(sid: str) -> dict:
    """The serialized strategy with its current value merged in, exactly what `build_snapshot` takes."""
    out = service.strategy_out(service.get_strategy(sid))
    try:
        out["value_usd"] = service.strategy_value(sid)["value_usd"]
    except Exception:
        out["value_usd"] = None
    return out


def _d(x) -> D | None:
    return None if x is None else D(str(x))


def maintenance_ratio(view: dict) -> D | None:
    pos = view.get("position") or {}
    maint, equity = _d(pos.get("maintenance_margin_usd")), _d(view.get("value_usd"))
    return None if not maint or equity is None else equity / maint


def top_up_amount(view: dict) -> str | None:
    """Whole dollars that bring maintenance_ratio to 2.0 (and cover any shortfall against the required margin). None if none needed."""
    pos = view.get("position") or {}
    maint, equity = _d(pos.get("maintenance_margin_usd")), _d(view.get("value_usd"))
    if not maint or equity is None:
        return None
    need = TARGET_RATIO * maint - equity
    required, margin = _d(view.get("required_margin_usd")), _d(pos.get("margin_usd"))
    if required is not None and margin is not None:
        need = max(need, required - margin)
    return None if need <= 0 else str(max(1, math.ceil(need)))


def return_excess_amount(view: dict) -> str | None:
    """Whole dollars of margin above 1.5x the required margin; None if there is none to return."""
    pos = view.get("position") or {}
    margin, required = _d(pos.get("margin_usd")), _d(view.get("required_margin_usd"))
    if margin is None or not required:
        return None
    excess = margin - EXCESS_FLOOR * required
    return None if excess < 1 else str(math.floor(excess))


def rebalance_params(view: dict) -> tuple[dict | None, str | None]:
    """({target_size}, None) or (None, why not). The trade must be worth the venue's minimum order."""
    pos = view.get("position") or {}
    gap, mark = _d(view.get("hedge_gap_units")), _d(pos.get("mark_price_usd"))
    if not gap:
        return None, "the hedge is already at its target"
    if mark and abs(gap) * mark < settings.min_order_usd * D("1.05"):
        return None, f"the change ({abs(gap)} oz, about ${abs(gap) * mark:.2f}) is below the venue's ${settings.min_order_usd} minimum order"
    return {"target_size": format(_d(view["target_hedge_size_units"]).normalize(), "f")}, None


def draft(view: dict, kind: str) -> tuple[dict | None, str]:
    """(action_draft | None, plain-text explanation). kind: rebalance | top_up | return_excess."""
    if kind == "rebalance":
        params, why = rebalance_params(view)
        if not params:
            return None, f"No rebalance: {why}."
        return {"type": "rebalance", "params": params,
                "summary": f"Rebalance the hedge to {params['target_size']} oz (gap {view.get('hedge_gap_units')} oz)"}, "Drafted a rebalance."
    if kind == "top_up":
        amt = top_up_amount(view)
        if not amt:
            return None, "No top-up needed: margin is comfortably above maintenance."
        return {"type": "top_up", "params": {"amount_usd": amt},
                "summary": f"Top up ${amt} to bring equity back to 2x maintenance margin"}, "Drafted a top-up."
    if kind == "return_excess":
        amt = return_excess_amount(view)
        if not amt:
            return None, "No excess margin to return: margin is within 1.5x of the requirement."
        return {"type": "return_excess", "params": {"amount_usd": amt},
                "summary": f"Return ${amt} of excess margin (leaves 1.5x the required margin)"}, "Drafted a return of excess margin."
    return None, "Only rebalance, top_up and return_excess can be drafted here."
