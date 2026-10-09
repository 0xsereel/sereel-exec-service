"""Delegation: an owner lets one key (the agent's) sign `rebalance` on one strategy, within limits the owner signed.

A delegate can do exactly one thing: rebalance the hedge toward the strategy's OWN target (a rebalance carries no parameters, so it
cannot pick a target). It cannot top up, withdraw, close, edit, change the owner, grant or revoke anything, publish NAV, run the agent
or configure the data feed: those need the owner's signature, and `auth.authorize` refuses a delegate on every other action. Limits
are enforced here, on the server, on every delegate request; they are never left to the delegate's good behaviour."""
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlmodel import Session, select

from . import solana_client as sol
from .db import engine
from .errors import ServiceError
from .models import Action, Delegate, now

D = Decimal
MAX_GRANT_DAYS = 30
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
GRANT_FIELDS = ("delegate_pubkey", "allowed_actions", "max_rebalance_oz_per_day", "rebalance_within_band_only", "expires_at")


def _bad(msg: str) -> ServiceError:
    return ServiceError("BAD_REQUEST", msg, 400)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_grant(params: dict, owner_pubkey: str | None, at: datetime | None = None) -> tuple[str, D, bool, datetime]:
    """Validate a grant's flat string params. Returns (delegate_pubkey, max_oz, band_only, expires_at)."""
    at = at or now()
    missing = [f for f in GRANT_FIELDS if f not in params]
    if missing:
        raise _bad(f"missing: {', '.join(missing)}")
    pub = params["delegate_pubkey"]
    if not sol.is_valid_address(pub):
        raise _bad("delegate_pubkey is not a valid Solana address")
    if owner_pubkey and pub == owner_pubkey:
        raise _bad("the owner does not need a delegate grant for its own key")
    if params["allowed_actions"] != "rebalance":
        raise _bad('allowed_actions must be exactly "rebalance": a delegate can do nothing else')
    try:
        max_oz = D(params["max_rebalance_oz_per_day"])
    except InvalidOperation:
        raise _bad("max_rebalance_oz_per_day must be a decimal string")
    if max_oz <= 0:
        raise _bad("max_rebalance_oz_per_day must be positive")
    if params["rebalance_within_band_only"] not in ("true", "false"):
        raise _bad('rebalance_within_band_only must be "true" or "false"')
    text = params["expires_at"]
    if not ISO_RE.match(text):
        raise _bad('expires_at must be an ISO 8601 UTC time such as "2026-10-15T09:00:00Z"')
    try:
        exp = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise _bad("expires_at is not a real date")
    if exp <= at:
        raise _bad("expires_at must be in the future")
    if exp > at + timedelta(days=MAX_GRANT_DAYS):
        raise _bad(f"expires_at must be at most {MAX_GRANT_DAYS} days from now")
    return pub, max_oz, params["rebalance_within_band_only"] == "true", exp


def status(g: Delegate, at: datetime | None = None) -> str:
    if g.revoked_at is not None:
        return "revoked"
    return "expired" if g.expires_at <= (at or now()) else "active"


def out(g: Delegate) -> dict:
    """The contract row. The two limits are echoed exactly as signed, as strings (the one exception to JSON booleans)."""
    return {"delegate_pubkey": g.delegate_pubkey, "allowed_actions": g.allowed_actions, "max_rebalance_oz_per_day": g.max_rebalance_oz_per_day,
            "rebalance_within_band_only": g.rebalance_within_band_only, "expires_at": iso(g.expires_at), "granted_at": iso(g.granted_at),
            "status": status(g), "attestation_sig": g.attestation_sig, "revoked_at": iso(g.revoked_at),
            "revoke_attestation_sig": g.revoke_attestation_sig}


def list_for(strategy_id: str) -> list[Delegate]:
    with Session(engine) as s:
        return list(s.exec(select(Delegate).where(Delegate.strategy_id == strategy_id).order_by(Delegate.granted_at.desc(), Delegate.id)).all())


def latest(strategy_id: str, pubkey: str) -> Delegate | None:
    """The newest grant row for this key on this strategy, whatever its status."""
    with Session(engine) as s:
        return s.exec(select(Delegate).where(Delegate.strategy_id == strategy_id, Delegate.delegate_pubkey == pubkey)
                      .order_by(Delegate.granted_at.desc(), Delegate.id)).first()


def active_grant(strategy_id: str, pubkey: str) -> Delegate | None:
    g = latest(strategy_id, pubkey)
    return g if g is not None and status(g) == "active" else None


def grant_for_signer(strategy_id: str, signer: str) -> Delegate | None:
    """The grant a verified signer is acting under, or None if it is the owner (or the dev bypass)."""
    if signer == "dev-bypass":
        return None
    return latest(strategy_id, signer)


def create(strategy_id: str, pub: str, max_oz: str, band_only: str, expires_at: datetime, granted_by: str) -> Delegate:
    """Store a grant. An older active grant to the same key is replaced (kept as revoked history)."""
    with Session(engine) as s:
        for old in s.exec(select(Delegate).where(Delegate.strategy_id == strategy_id, Delegate.delegate_pubkey == pub,
                                                 Delegate.revoked_at.is_(None))).all():
            old.revoked_at = now()
            s.add(old)
        g = Delegate(strategy_id=strategy_id, delegate_pubkey=pub, max_rebalance_oz_per_day=max_oz, rebalance_within_band_only=band_only,
                     expires_at=expires_at, granted_by=granted_by)
        s.add(g)
        s.commit()
        s.refresh(g)
        return g


def revoke(grant_id: str) -> Delegate:
    with Session(engine) as s:
        g = s.get(Delegate, grant_id)
        g.revoked_at = now()
        s.add(g)
        s.commit()
        s.refresh(g)
        return g


def set_attestation(grant_id: str, sig: str | None, revoke: bool = False) -> None:
    with Session(engine) as s:
        g = s.get(Delegate, grant_id)
        if revoke:
            g.revoke_attestation_sig = sig
        else:
            g.attestation_sig = sig
        s.add(g)
        s.commit()


# ---- limits -------------------------------------------------------------------------------------------------------------------------

def traded_last_24h(strategy_id: str, pubkey: str, at: datetime | None = None) -> D:
    """Oz this delegate has actually moved on this strategy in the last 24 hours, from the recorded rebalances."""
    at = at or now()
    total = D(0)
    with Session(engine) as s:
        for a in s.exec(select(Action).where(Action.strategy_id == strategy_id, Action.action == "rebalance", Action.signer_public_key == pubkey,
                                            Action.created_at > at - timedelta(hours=24))).all():
            r = a.record or {}
            if r.get("traded"):
                total += abs(D(str((r.get("from") or {}).get("size", 0))) - D(str(r.get("size_after", 0))))
    return total


def check_rebalance(grant: Delegate, strategy_id: str, force: bool, size_oz: D) -> None:
    """Raise DELEGATE_LIMIT_EXCEEDED if this rebalance is outside what the owner signed."""
    if grant.rebalance_within_band_only == "true" and force:
        raise ServiceError("DELEGATE_LIMIT_EXCEEDED", "this grant allows rebalancing within the band only: a forced rebalance needs the owner's signature", 403)
    done, cap = traded_last_24h(strategy_id, grant.delegate_pubkey), D(grant.max_rebalance_oz_per_day)
    if done + size_oz > cap:
        raise ServiceError("DELEGATE_LIMIT_EXCEEDED", f"this rebalance ({size_oz.normalize():f} oz) plus {done.normalize():f} oz already moved in the last "
                           f"24 hours exceeds the grant's limit of {cap.normalize():f} oz per day", 403)
