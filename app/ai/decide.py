"""Probabilities and state in, at most one Decision out. Plain code: every number in `action.params` is computed here from the
strategy's own state (see actions.py), never taken from a model. `decide` only PROPOSES; whether something executes is the delegation
layer's business, and even then the downgrade rules below can turn an `execute` back into a `propose`.

Order of precedence: hold (a market-safety stop) beats everything, then top_up, rebalance, return_excess."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from ..config import settings
from . import actions
from .state import Snapshot

D = Decimal
HOLD_QUESTIONS = ("venue_price_divergence", "abnormal_price_move")
LIQUIDITY_FLOOR = D("0.5")  # a rebalance also needs liquidity_sufficient at least this


@dataclass
class Decision:
    kind: str  # none | hold | suggest | propose | execute
    action: dict | None = None  # {"type", "params"}
    reason: str = ""  # which rule fired, one deterministic line
    downgraded_from: str | None = None


def _level(p: D | None, act: D, suggest: D) -> str | None:
    if p is None:
        return None
    return "act" if p >= act else "suggest" if p >= suggest else None


def _equity_over_required(view: dict) -> bool:
    eq, req = view.get("value_usd"), view.get("required_margin_usd")
    return eq is not None and req is not None and D(str(eq)) > 2 * D(str(req))


def decide(snap: Snapshot, view: dict, p: dict[str, D], *, act: D | None = None, suggest: D | None = None, delegated: bool = False,
           now: datetime | None = None, last_executed_at: datetime | None = None) -> Decision:
    act = settings.agent_act_threshold if act is None else act
    suggest = settings.agent_suggest_threshold if suggest is None else suggest
    # hard caps: nothing for a strategy that is not live or has no owner to answer to
    if view.get("status") != "active":
        return Decision("none", reason=f"strategy is {view.get('status')}: no action")
    if not view.get("owner_pubkey") and not view.get("owner_multisig"):
        return Decision("none", reason="strategy has no owner: no action")

    # 1. hold wins over everything: do not trade into a market the price sources disagree about or that is moving abnormally
    worst = max(HOLD_QUESTIONS, key=lambda q: p.get(q, D(0)))
    lvl = _level(p.get(worst), act, suggest)
    if lvl:
        reason = f"{worst} = {p[worst]:.2f}: do not trade this cycle"
        return Decision("hold" if lvl == "act" else "suggest", {"type": "hold", "params": {}}, reason)

    # 2-4. the rest, first match wins
    candidates: list[tuple[str, str, dict, str]] = []  # (question, level, action, reason)
    lvl = _level(p.get("needs_top_up_soon"), act, suggest)
    amount = actions.top_up_amount(view) if lvl else None
    if lvl and amount:
        candidates.append(("needs_top_up_soon", lvl, {"type": "top_up", "params": {"amount_usd": amount}},
                           f"needs_top_up_soon = {p['needs_top_up_soon']:.2f}"))
    lvl = _level(p.get("should_rebalance"), act, suggest)
    if lvl and p.get("liquidity_sufficient", D(1)) >= LIQUIDITY_FLOOR:
        params, why = actions.rebalance_params(view)
        if params:
            candidates.append(("should_rebalance", lvl, {"type": "rebalance", "params": params},
                               f"should_rebalance = {p['should_rebalance']:.2f}, liquidity_sufficient = {p.get('liquidity_sufficient', D(1)):.2f}"))
    lvl = _level(p.get("excess_margin_safe_to_return"), act, suggest)
    amount = actions.return_excess_amount(view) if lvl and _equity_over_required(view) else None
    if lvl and amount:
        candidates.append(("excess_margin_safe_to_return", lvl, {"type": "return_excess", "params": {"amount_usd": amount}},
                           f"excess_margin_safe_to_return = {p['excess_margin_safe_to_return']:.2f}"))
    if not candidates:
        return Decision("none", reason="no rule fired")
    _, lvl, action, reason = candidates[0]
    if lvl == "suggest":
        return Decision("suggest", action, reason + " (between the suggest and act thresholds)")
    kind = "propose"
    if action["type"] == "rebalance" and delegated:  # only a delegated rebalance may execute; money movements never do
        kind = "execute"
    if kind == "execute":
        kind, reason, down = _downgrade(snap, now, last_executed_at, reason)
        return Decision(kind, action, reason, "execute" if kind != "execute" else None)
    return Decision(kind, action, reason)


def _downgrade(snap: Snapshot, now: datetime | None, last_executed_at: datetime | None, reason: str) -> tuple[str, str, bool]:
    """execute -> propose when the venue the order would hit is not trustworthy right now, or the 10-minute cap was used."""
    bps = snap.get("execution", "testnet_mark_vs_pyth_bps")
    if bps is not None and abs(bps) > settings.agent_exec_max_divergence_bps:
        return "propose", f"{reason}; downgraded to a proposal: the execution venue is {abs(bps):.1f} bps from Pyth (limit {settings.agent_exec_max_divergence_bps})", True
    if last_executed_at and now and now - last_executed_at < timedelta(seconds=settings.agent_exec_cap_s):
        return "propose", f"{reason}; downgraded to a proposal: an action was already executed in the last {settings.agent_exec_cap_s // 60} minutes", True
    return "execute", reason, False
