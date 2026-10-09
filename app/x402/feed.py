"""What is sold, and ONLY what is sold: post-trade, verified data. Nothing that reveals an upcoming trade.

The feed is assembled by an explicit allow-list: each sellable field is built from scratch out of a few named inputs. Nothing is copied
from a bigger object and filtered afterwards, so a field added to the strategy tomorrow cannot leak into a sale by default.

Never sold, on any x402 response or the preview: agent signals (Jev probabilities), pending decisions or proposals, live position size,
target size, pending rebalances, delegate grants, margin health, liquidation price. (NEVER_EXPOSE lists them for the tests.)"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlmodel import Session, select

from .. import solana_client as sol
from ..config import settings
from ..db import engine
from ..errors import ServiceError
from ..models import Action, DataFeed, DataPayment, NavCheckpoint, Strategy, now

SELLABLE = ("nav_per_share", "nav_history", "hedge_summary", "attestations")
# executed, post-trade events only. Edits, grants, decisions, owner changes and ledger notes are not on this list on purpose.
EXECUTED_ACTIONS = ("deploy", "deposit", "rebalance", "return_excess", "close", "publish_nav")
NEVER_EXPOSE = ("signals", "probabilities", "decision", "decisions", "proposal", "proposals", "position", "size_units", "target_size",
                "target_hedge_size_units", "target_exposure_units", "hedge_gap_units", "hedge_gap_bps", "pending", "delegate", "delegates",
                "grant", "margin_health", "margin_health_bps", "liquidation", "liquidation_price_usd", "maintenance_margin_usd",
                "required_margin_usd", "margin_usd", "entry_price_usd", "unrealized_pnl_usd", "state_hash")
HISTORY_LIMIT = 100
ATTESTATION_LIMIT = 100


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def money_str(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    if "." not in s:
        return s + ".00"
    return s + "0" * max(0, 2 - len(s.split(".")[1]))


def parse_fields(text: str) -> list[str]:
    """A comma-separated subset of the sellable names, in the canonical order, no duplicates. BAD_REQUEST otherwise."""
    parts = [p.strip() for p in text.split(",")]
    if not text.strip() or any(not p for p in parts):
        raise ServiceError("BAD_REQUEST", f"fields must be a comma-separated list of: {', '.join(SELLABLE)}", 400)
    bad = [p for p in parts if p not in SELLABLE]
    if bad:
        raise ServiceError("BAD_REQUEST", f"fields may only contain: {', '.join(SELLABLE)} (not: {', '.join(bad)})", 400)
    if len(set(parts)) != len(parts):
        raise ServiceError("BAD_REQUEST", "fields lists a name twice", 400)
    return [f for f in SELLABLE if f in parts]


def config(strategy_id: str) -> DataFeed | None:
    with Session(engine) as s:
        return s.get(DataFeed, strategy_id)


def active_config(strategy_id: str) -> DataFeed | None:
    """The config of an enabled feed on an existing strategy, else None. Unknown id and disabled feed are the same answer."""
    with Session(engine) as s:
        cfg = s.get(DataFeed, strategy_id)
        if cfg is None or not cfg.enabled or not cfg.pay_to or not cfg.price_usd or s.get(Strategy, strategy_id) is None:
            return None
        return cfg


def _unavailable(reason: str) -> dict:
    return {"status": "unavailable", "reason": reason}


def _checkpoints(strategy_id: str, limit: int) -> list[NavCheckpoint]:
    with Session(engine) as s:
        return list(s.exec(select(NavCheckpoint).where(NavCheckpoint.strategy_id == strategy_id)
                           .order_by(NavCheckpoint.as_of.desc(), NavCheckpoint.created_at.desc()).limit(limit)).all())


def latest_checkpoint(strategy_id: str) -> NavCheckpoint | None:
    cps = _checkpoints(strategy_id, 1)
    return cps[0] if cps else None


def executed_settings(strategy_id: str) -> tuple[Decimal, Decimal, Decimal]:
    """(hedge ratio bps, declared exposure, size held) as of the last EXECUTED trade, never the strategy's current settings.

    An owner's edit changes the target immediately but trades nothing until a rebalance, so reading the live settings would announce the
    next trade. Settings changed after the last executed trade are rolled back to what they were (every edit records its `from`)."""
    with Session(engine) as s:
        st = s.get(Strategy, strategy_id)
        ratio, exposure, held = Decimal(st.hedge_ratio_bps), st.target_exposure_units, abs(st.size)
        trades = [a for a in s.exec(select(Action).where(Action.strategy_id == strategy_id, Action.action.in_(("deploy", "rebalance")))
                                    .order_by(Action.created_at.desc(), Action.id)).all()
                  if a.action == "deploy" or (a.record or {}).get("traded")]
        since = trades[0].created_at if trades else None
        edits = s.exec(select(Action).where(Action.strategy_id == strategy_id, Action.action == "edit_hedge_settings")
                       .order_by(Action.created_at, Action.id)).all()
    pending = [e for e in edits if since is None or e.created_at > since]
    if pending:  # the earliest unexecuted edit's `from` is what was true at the last trade
        before = (pending[0].record or {}).get("from") or {}
        ratio = Decimal(str(before.get("hedge_ratio_bps", ratio)))
        exposure = Decimal(str(before.get("target_exposure_units", exposure)))
    return ratio, exposure, held


def build(strategy_id: str, fields: list[str], at: datetime | None = None) -> dict:
    """The JSON a paying buyer receives right now. Built field by field from the allow-list."""
    at = at or now()
    out: dict = {"strategy_id": strategy_id, "as_of": iso(at)}
    if "nav_per_share" in fields:
        cp = latest_checkpoint(strategy_id)
        out["nav_per_share"] = _unavailable("no NAV has been published for this strategy yet") if cp is None else \
            {"status": "available", "hedged": cp.nav_per_share, "unhedged": cp.unhedged_nav_per_share, "as_of": iso(cp.as_of)}
    if "nav_history" in fields:
        cps = _checkpoints(strategy_id, HISTORY_LIMIT)
        out["nav_history"] = _unavailable("no NAV has been published for this strategy yet") if not cps else \
            {"status": "available", "checkpoints": [{"as_of": iso(c.as_of), "hedged": c.nav_per_share, "unhedged": c.unhedged_nav_per_share} for c in cps]}
    if "hedge_summary" in fields:
        ratio, exposure, held = executed_settings(strategy_id)
        share = (held / exposure * 100) if exposure else Decimal(0)
        out["hedge_summary"] = {"hedge_ratio_pct": f"{ratio / 100:.2f}", "hedged_share_of_exposure_pct": f"{share:.2f}"}
    if "attestations" in fields:
        delay = settings.x402_attestation_delay_s
        cutoff = at - timedelta(seconds=delay)
        with Session(engine) as s:
            rows = s.exec(select(Action).where(Action.strategy_id == strategy_id, Action.action.in_(EXECUTED_ACTIONS), Action.attestation_sig.is_not(None),
                                               Action.created_at <= cutoff).order_by(Action.created_at.desc(), Action.id).limit(ATTESTATION_LIMIT)).all()
            items = [{"action": a.action, "at": iso(a.created_at), "attestation_sig": a.attestation_sig, "explorer_url": sol.explorer_url(a.attestation_sig)}
                     for a in rows]
        out["attestations"] = {"delay_s": delay, "items": items}
    return out


# ---- income -------------------------------------------------------------------------------------------------------------------------------

def income(strategy_id: str) -> tuple[Decimal, int, datetime | None]:
    with Session(engine) as s:
        rows = s.exec(select(DataPayment).where(DataPayment.strategy_id == strategy_id)).all()
    return sum((r.amount_usd for r in rows), Decimal(0)), len(rows), max((r.settled_at for r in rows), default=None)


def payments(strategy_id: str, limit: int = 50) -> list[dict]:
    with Session(engine) as s:
        rows = s.exec(select(DataPayment).where(DataPayment.strategy_id == strategy_id).order_by(DataPayment.settled_at.desc(), DataPayment.id)
                      .limit(max(1, min(limit, 200)))).all()
    return [{"payer": r.payer, "amount_usd": money_str(r.amount_usd), "mint": r.mint, "tx_signature": r.tx_signature, "settled_at": iso(r.settled_at),
             "fields_served": r.fields_served} for r in rows]
