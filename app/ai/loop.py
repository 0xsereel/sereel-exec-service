"""The monitoring cycle: snapshot -> Jev (or rules) -> decide -> explain -> store. Proposals only in this step: nothing here moves
money. One strategy's failure never stops the others."""
import logging
import threading
from decimal import Decimal
import time

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..errors import ServiceError
from ..models import S_ACTIVE, AgentDecision, Strategy, now
from ..state import state
from .. import delegates
from ..strategies import service
from . import actions, agent_key, decisions, signal_log
from .decide import Decision, decide
from .explain import explain
from .signals import get_signals
from .state import build_snapshot

D = Decimal
log = logging.getLogger("sereel.agent")
_lock = threading.Lock()
_last_run_once: dict[str, float] = {}
last_cycle: dict = {"at": None}


def _last_executed_at(sid: str):
    with Session(engine) as s:
        return s.exec(select(AgentDecision.at).where(AgentDecision.strategy_id == sid, AgentDecision.outcome == "executed",
                                                     AgentDecision.decision == "execute").order_by(AgentDecision.at.desc())).first()


OWNER_OUTCOMES = ("DELEGATE_LIMIT_EXCEEDED", "DELEGATE_NOT_ALLOWED", "AUTHORIZATION_INVALID", "AUTHORIZATION_REQUIRED")  # refused, not failed


def run_strategy(sid: str) -> AgentDecision:
    """One full cycle for one strategy. Raises SIGNALS_UNAVAILABLE when there is not enough market data to decide on."""
    view = actions.strategy_view(sid)
    market = state.markets[view["market_id"]]
    snap = build_snapshot(market, view)
    if "price" not in snap.sections and "venue" not in snap.sections:
        raise ServiceError("SIGNALS_UNAVAILABLE", "neither Pyth nor Hyperliquid could be read, so no decision was taken this cycle", 503)
    sig = get_signals(snap)
    delegated = decisions.has_active_delegate(sid)
    d = decide(snap, view, sig.probabilities, delegated=delegated, now=now(), last_executed_at=_last_executed_at(sid))
    if d.kind == "execute":
        d = _within_the_grant(sid, d, view)  # the grant's limits, checked before anything is signed
    explanation = explain(d, view, snap, sig.probabilities)
    row = decisions.store(sid, state_hash=snap.state_hash, signals_source=sig.source, signals_network=snap.signals_network,
                          question_set_version=sig.question_set_version,
                          signals={k: f"{v:.2f}" for k, v in sorted(sig.probabilities.items())},
                          decision=d.kind, action=d.action, explanation=explanation, reason=d.reason, downgraded_from=d.downgraded_from)
    signal_log.record(sid, snap, sig, d)  # every Jev answer and the decision, one line per cycle, plus the rotating JSON file
    if d.kind == "execute":
        row = _execute(sid, row, snap, sig)
    return row


def _within_the_grant(sid: str, d: Decision, view: dict) -> Decision:
    """execute -> propose if this rebalance would break the owner's signed limits (the server enforces them again on the request)."""
    grant = delegates.active_grant(sid, agent_key.pubkey())
    gap = abs(D(str(view.get("hedge_gap_units") or 0)))
    try:
        if grant is None:
            raise ServiceError("DELEGATE_NOT_ALLOWED", "the agent has no active grant on this strategy", 403)
        delegates.check_rebalance(grant, sid, False, gap)
        return d
    except ServiceError as e:
        return Decision("propose", d.action, f"{d.reason}; downgraded to a proposal: {e.message}", "execute")


def _execute(sid: str, row: AgentDecision, snap, sig) -> AgentDecision:
    """Sign a rebalance with the agent's key and send it through the normal service path (locks, nonce replay, limits, attestation)."""
    meta = {"decision_id": row.id, "state_hash": snap.state_hash, "signals_source": sig.source, "question_set_version": sig.question_set_version,
            "model": sig.jev_result.model if sig.jev_result else "rules", "signals": {k: f"{v:.2f}" for k, v in sorted(sig.probabilities.items())},
            "trigger": {"sr": f"{sig.probabilities['should_rebalance']:.2f}", "ls": f"{sig.probabilities.get('liquidity_sufficient', D(0)):.2f}"}}
    try:
        authorization = agent_key.sign_authorization("rebalance", sid, {})
        service.rebalance(sid, authorization, False, "", agent_meta=meta)
    except ServiceError as e:
        decisions.finish(row.id, "rejected" if e.code in OWNER_OUTCOMES else "failed", reason_note=f"execution refused: {e.code}: {e.message}")
        log.warning("agent rebalance on %s did not run: %s %s", sid[:8], e.code, e.message)
        return decisions.get(row.id)
    except Exception as e:
        decisions.finish(row.id, "failed", reason_note=f"execution error: {type(e).__name__}")
        log.exception("agent rebalance on %s crashed", sid[:8])
        return decisions.get(row.id)
    act = decisions.latest_action(sid, "rebalance")
    traded = bool(act and (act.record or {}).get("traded"))
    if not traded:  # the gap closed or fell inside the band between the decision and the order
        decisions.finish(row.id, "failed", reason_note="nothing traded: the hedge was already at target by the time the order was sent",
                         action_id=act.id if act else None)
    else:
        decisions.finish(row.id, "executed", action_id=act.id, attestation_sig=act.attestation_sig)
    return decisions.get(row.id)


def run_cycle() -> int:
    """Every active strategy once. Returns how many produced a decision."""
    if not _lock.acquire(blocking=False):
        return 0  # the previous cycle is still running
    n = 0
    try:
        with Session(engine) as s:
            ids = [r for r in s.exec(select(Strategy.id).where(Strategy.status == S_ACTIVE)).all()]
        for sid in ids:
            try:
                run_strategy(sid)
                n += 1
            except Exception as e:
                code = getattr(e, "code", type(e).__name__)
                log.warning("agent cycle for %s failed: %s", sid[:8], code)
                signal_log.record_failure(sid, code, str(getattr(e, "message", e))[:200])
        last_cycle["at"] = now()
        return n
    finally:
        _lock.release()


def run_once(sid: str) -> AgentDecision:
    """The owner-signed 'run now' (for the demo). Inside the cooldown it returns the latest decision and calls nothing."""
    t = time.monotonic()
    prev = _last_run_once.get(sid)
    if prev is not None and t - prev < settings.agent_run_once_cooldown_s:
        latest = decisions.latest(sid)
        if latest is not None:
            return latest
    _last_run_once[sid] = t
    return run_strategy(sid)


def register(sched) -> None:
    if not settings.agent_enabled:
        return

    def tick():
        try:
            run_cycle()
        except Exception:
            log.exception("agent cycle failed (will retry)")

    sched.add_job(tick, "interval", seconds=max(10, settings.agent_interval_s), id="agent-cycle", max_instances=1, coalesce=True,
                  misfire_grace_time=30)
