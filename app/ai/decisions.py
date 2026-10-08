"""Storage and serialization of agent decisions, and the link back from an owner's action.

No decision moves money. A proposal waits (outcome `pending`) until the owner acts, dismisses it, or a newer proposal of the same type
replaces it. Cantina's Approve is just the prefilled modal: when the owner then executes the same action type, `resolve_executed`
marks the proposal `executed` and links the action, with no extra call."""
from datetime import datetime, timezone

from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import Action, AgentDecision, now

OPEN = ("suggest", "propose")  # decisions that wait for the owner
# the owner's action name (Action.action) for each action type a proposal can carry
ACTION_NAMES = {"rebalance": "rebalance", "top_up": "deposit", "return_excess": "return_excess"}


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def out(d: AgentDecision) -> dict:
    """The AgentDecision contract. Every field is always present (null, never missing)."""
    ex = d.explanation or {}
    return {"id": d.id, "at": iso(d.at), "strategy_id": d.strategy_id, "state_hash": d.state_hash, "signals_source": d.signals_source,
            "signals_network": d.signals_network, "question_set_version": d.question_set_version, "signals": dict(d.signals or {}),
            "decision": d.decision, "action": d.action, "reason": d.reason or None, "downgraded_from": d.downgraded_from,
            "explanation": {"headline": ex.get("headline"), "explanation": ex.get("explanation"), "risk_note": ex.get("risk_note")},
            "outcome": d.outcome, "action_id": d.action_id, "attestation_sig": d.attestation_sig}


def latest(strategy_id: str) -> AgentDecision | None:
    with Session(engine) as s:
        return s.exec(select(AgentDecision).where(AgentDecision.strategy_id == strategy_id)
                      .order_by(AgentDecision.at.desc(), AgentDecision.id)).first()


def list_for(strategy_id: str, limit: int = 50) -> list[dict]:
    with Session(engine) as s:
        rows = s.exec(select(AgentDecision).where(AgentDecision.strategy_id == strategy_id)
                      .order_by(AgentDecision.at.desc(), AgentDecision.id).limit(max(1, min(limit, 200)))).all()
    return [out(r) for r in rows]


def last_cycle_at() -> datetime | None:
    with Session(engine) as s:
        return s.exec(select(AgentDecision.at).order_by(AgentDecision.at.desc())).first()


def has_unread_proposal(strategy_id: str) -> bool:
    with Session(engine) as s:
        return s.exec(select(AgentDecision.id).where(AgentDecision.strategy_id == strategy_id, AgentDecision.decision.in_(OPEN),
                                                     AgentDecision.outcome == "pending")).first() is not None


def has_active_delegate(strategy_id: str) -> bool:
    return False  # replaced by the delegates table in the delegation step


def agent_mode(strategy_id: str) -> str | None:
    """autopilot = agent on and an active delegate grant; monitoring = agent on, no grant; None = agent off."""
    if not settings.agent_enabled:
        return None
    return "autopilot" if has_active_delegate(strategy_id) else "monitoring"


def store(strategy_id: str, *, state_hash: str, signals_source: str, signals_network: str, question_set_version: str, signals: dict,
          decision: str, action: dict | None, explanation: dict, reason: str, downgraded_from: str | None = None) -> AgentDecision:
    """Save a cycle's outcome. `none` updates the previous heartbeat in place (the feed is not flooded); a new suggestion or
    proposal dismisses an older pending one of the same action type."""
    with Session(engine) as s:
        prev = s.exec(select(AgentDecision).where(AgentDecision.strategy_id == strategy_id)
                      .order_by(AgentDecision.at.desc(), AgentDecision.id)).first()
        fields = dict(state_hash=state_hash, signals_source=signals_source, signals_network=signals_network, signals=signals,
                      question_set_version=question_set_version, decision=decision, action=action, explanation=explanation,
                      reason=reason, downgraded_from=downgraded_from, at=now())
        if decision == "none" and prev is not None and prev.decision == "none" and prev.outcome == "pending":
            for k, v in fields.items():
                setattr(prev, k, v)
            row = prev
        else:
            row = AgentDecision(strategy_id=strategy_id, **fields)
        s.add(row)
        if decision in OPEN and action:
            for old in s.exec(select(AgentDecision).where(AgentDecision.strategy_id == strategy_id, AgentDecision.decision.in_(OPEN),
                                                          AgentDecision.outcome == "pending")).all():
                if old.id != row.id and (old.action or {}).get("type") == action["type"]:
                    old.outcome = "dismissed"
                    s.add(old)
        s.commit()
        s.refresh(row)
        return row


def dismiss_lookup(strategy_id: str, decision_id: str) -> AgentDecision | None:
    """The decision if it exists on this strategy (404 first, so an unknown id never burns a nonce)."""
    with Session(engine) as s:
        d = s.get(AgentDecision, decision_id)
    return d if d is not None and d.strategy_id == strategy_id else None


def dismiss(strategy_id: str, decision_id: str) -> AgentDecision | None:
    with Session(engine) as s:
        d = s.get(AgentDecision, decision_id)
        if d is None or d.strategy_id != strategy_id:
            return None
        if d.outcome == "pending" and d.decision in OPEN:
            d.outcome = "dismissed"
            s.add(d)
            s.commit()
            s.refresh(d)
        return d


def resolve_executed(strategy_id: str, action_type: str) -> int:
    """The owner executed `action_type` (rebalance | top_up | return_excess): mark the pending proposal of that type `executed` and link
    the action just recorded. Returns how many were resolved. Never raises into the caller's money path."""
    try:
        name = ACTION_NAMES[action_type]
        with Session(engine) as s:
            act = s.exec(select(Action).where(Action.strategy_id == strategy_id, Action.action == name)
                         .order_by(Action.created_at.desc(), Action.id)).first()
            rows = s.exec(select(AgentDecision).where(AgentDecision.strategy_id == strategy_id, AgentDecision.decision.in_(OPEN),
                                                      AgentDecision.outcome == "pending")).all()
            n = 0
            for d in rows:
                if (d.action or {}).get("type") == action_type:
                    d.outcome, d.action_id = "executed", act.id if act else None
                    s.add(d)
                    n += 1
            s.commit()
            return n
    except Exception:  # bookkeeping must never break a trade or a withdrawal
        return 0
