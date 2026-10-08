"""The monitoring cycle: snapshot -> Jev (or rules) -> decide -> explain -> store. Proposals only in this step: nothing here moves
money. One strategy's failure never stops the others."""
import logging
import threading
import time

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..errors import ServiceError
from ..models import S_ACTIVE, AgentDecision, Strategy, now
from ..state import state
from . import actions, decisions
from .decide import decide
from .explain import explain
from .signals import get_signals
from .state import build_snapshot

log = logging.getLogger("sereel.agent")
_lock = threading.Lock()
_last_run_once: dict[str, float] = {}
last_cycle: dict = {"at": None}


def _last_executed_at(sid: str):
    with Session(engine) as s:
        return s.exec(select(AgentDecision.at).where(AgentDecision.strategy_id == sid, AgentDecision.outcome == "executed",
                                                     AgentDecision.decision == "execute").order_by(AgentDecision.at.desc())).first()


def run_strategy(sid: str) -> AgentDecision:
    """One full cycle for one strategy. Raises SIGNALS_UNAVAILABLE when there is not enough market data to decide on."""
    view = actions.strategy_view(sid)
    market = state.markets[view["market_id"]]
    snap = build_snapshot(market, view)
    if "price" not in snap.sections and "venue" not in snap.sections:
        raise ServiceError("SIGNALS_UNAVAILABLE", "neither Pyth nor Hyperliquid could be read, so no decision was taken this cycle", 503)
    sig = get_signals(snap)
    d = decide(snap, view, sig.probabilities, delegated=decisions.has_active_delegate(sid), now=now(), last_executed_at=_last_executed_at(sid))
    explanation = explain(d, view, snap, sig.probabilities)
    row = decisions.store(sid, state_hash=snap.state_hash, signals_source=sig.source, signals_network=snap.signals_network,
                          question_set_version=sig.question_set_version,
                          signals={k: f"{v:.2f}" for k, v in sorted(sig.probabilities.items())},
                          decision=d.kind, action=d.action, explanation=explanation, reason=d.reason, downgraded_from=d.downgraded_from)
    log.info("agent %s: %s%s (%s, signals %s)", sid[:8], d.kind, f" {d.action['type']}" if d.action else "", d.reason, sig.source)
    return row


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
                log.warning("agent cycle for %s failed: %s", sid[:8], getattr(e, "code", type(e).__name__))
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
