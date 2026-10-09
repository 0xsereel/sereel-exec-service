"""Watches the funding address for inbound stablecoin transfers (finalized only) and applies them.

A transfer funds an intent only if its memo equals the intent id AND its sender equals the intent's
registered_sender_address AND the intent is still pending and unexpired. Anything else (no memo, wrong sender, an
intent that was cancelled, expired or already activated) is refunded to its sender and attested. Transfers from the
service's own addresses are ignored. The cursor (newest finalized signature fully processed) lives in the database, so
a restart resumes where it stopped. Each transfer is claimed in the database before anything is done with it, so none
is ever credited or refunded twice.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .. import solana_client as sol
from ..db import engine
from ..models import (D_PENDING, S_PENDING, ChainTransfer, Strategy, StrategyDeposit, WatcherCursor, now)
from . import service, withdrawals

log = logging.getLogger("sereel.watcher")
CURSOR = "funding"
STALE_CLAIM_S = 120


def watch_address() -> str:
    """The address whose transaction list is read: the funding wallet's TOKEN ACCOUNT, not the wallet. A plain SPL transfer
    references the destination token account but not its owner, so listing the wallet misses every deposit into an account
    that already exists (only a transfer that also creates the account mentions the wallet). Every transfer into the token
    account references it, including the one that creates it."""
    return str(sol.ata(service.funding_address()))


def ensure_cursor() -> None:
    """First ever start: begin at the newest existing signature, so history that predates the service (old test
    transfers, SOL airdrops) is not mistaken for deposits and refunded."""
    with Session(engine) as s:
        if s.get(WatcherCursor, CURSOR):
            return
        page = sol.rpc("getSignaturesForAddress", [watch_address(), {"commitment": "finalized", "limit": 1}])
        s.add(WatcherCursor(name=CURSOR, last_signature=page[0]["signature"] if page else None))
        s.commit()
        log.info("deposit watcher starts after %s", page[0]["signature"] if page else "the beginning of time")


def _set_cursor(sig: str) -> None:
    with Session(engine) as s:
        c = s.get(WatcherCursor, CURSOR)
        c.last_signature, c.updated_at = sig, now()
        s.add(c)
        s.commit()


def _finish(sig: str, disposition: str, note: str | None = None, intent_id: str | None = None,
            refund_sig: str | None = None, attestation_sig: str | None = None) -> None:
    with Session(engine) as s:
        t = s.get(ChainTransfer, sig)
        t.disposition, t.note, t.intent_id = disposition, note, intent_id
        t.refund_signature, t.attestation_sig = refund_sig, attestation_sig
        s.add(t)
        s.commit()


def _match(sender: str, memo: str | None) -> tuple[str, str | None, str]:
    """-> (kind, id, why). kind is 'deploy' | 'deposit' when the transfer funds a live intent, else 'none'
    (id is the strategy the memo pointed at, if any, and why explains the refund)."""
    if not memo:
        return "none", None, "no memo: cannot be matched to an intent"
    with Session(engine) as s:
        st = s.exec(select(Strategy).where(Strategy.intent_id == memo)).first()
        if st:
            if st.registered_sender_address != sender:
                return "none", st.id, "sender is not the intent's registered_sender_address"
            if st.status != S_PENDING or st.expires_at < now():
                return "none", st.id, f"intent is no longer open (strategy {st.status})"
            return "deploy", st.id, ""
        dep = s.exec(select(StrategyDeposit).where(StrategyDeposit.intent_id == memo)).first()
        if dep:
            if dep.registered_sender_address != sender:
                return "none", dep.strategy_id, "sender is not the intent's registered_sender_address"
            if dep.status != D_PENDING or dep.expires_at < now():
                return "none", dep.strategy_id, f"intent is no longer open (deposit {dep.status})"
            return "deposit", dep.id, ""
    return "none", None, "memo does not match any intent"


def _refund_unmatched(sig: str, sender: str | None, amount: Decimal, strategy_id: str | None, why: str) -> None:
    if not sender:
        log.error("transfer %s (%s) cannot be refunded: sender unknown", sig, amount)
        _finish(sig, "refund_failed", f"sender unknown; {why}")
        return
    try:
        rsig = service.send_refund(sender, amount, sig)
    except Exception as e:
        log.error("REFUND FAILED for transfer %s (%s -> %s): %s", sig, amount, sender, e)
        _finish(sig, "refund_failed", f"{why}; refund error: {e}")
        return
    fund = "-"
    if strategy_id:
        with Session(engine) as s:
            st = s.get(Strategy, strategy_id)
            fund = st.fund_id if st else "-"
    asig = service._record_action(strategy_id or "unmatched", "refund",
                                  {"reason": why, "amount_usd": amount, "to": sender, "deposit_signature": sig,
                                   "refund_signature": rsig}, fund, rsig)
    _finish(sig, "refunded", why, refund_sig=rsig, attestation_sig=asig)
    log.info("refunded %s to %s: %s", amount, sender, why)


def process_signature(sig: str, funding: str) -> str | None:
    """Handle one finalized transfer. Returns the strategy id an activation was just attempted for, if any."""
    with Session(engine) as s:
        if s.get(ChainTransfer, sig):
            return
    tx = sol.get_parsed_tx(sig, "finalized")
    if tx is None:
        raise sol.SolanaError(f"transaction {sig} is not retrievable yet")
    inbound = sol.parse_inbound(tx, sig, funding)
    if not inbound:  # not an inbound stablecoin transfer (our own refund, a SOL transfer, ...)
        return
    sender, amount, memo = inbound["sender"], inbound["amount"], inbound["memo"]
    with Session(engine) as s:  # claim it: the primary key makes a second claim impossible
        s.add(ChainTransfer(signature=sig, sender=sender, amount_usd=amount, memo=memo, disposition="processing"))
        try:
            s.commit()
        except IntegrityError:
            return
    if sender in sol.own_addresses():
        _finish(sig, "ignored_own", "sent from one of the service's own addresses")
        return
    kind, ref, why = _match(sender, memo)
    if kind == "none":
        _refund_unmatched(sig, sender, amount, ref, why)
        return
    with Session(engine) as s:
        if kind == "deploy":
            st = s.get(Strategy, ref)
            st.received_amount_usd = (st.received_amount_usd or Decimal(0)) + amount
            st.deploy_signature, st.updated_at = sig, now()
            s.add(st)
            funded = st.received_amount_usd >= st.expected_amount_usd
            if funded and st.funded_at is None:
                st.funded_at = now()  # the deploy retry window starts here, once; later transfers never move it
        else:
            dep = s.get(StrategyDeposit, ref)
            dep.received_amount_usd = (dep.received_amount_usd or Decimal(0)) + amount
            dep.source_wallet_address, dep.solana_signature = sender, sig
            s.add(dep)
            funded = dep.received_amount_usd >= dep.expected_amount_usd
        t = s.get(ChainTransfer, sig)
        t.disposition, t.intent_id = "credited", memo
        s.add(t)
        s.commit()  # credit and disposition land together
    log.info("credited %s to %s %s (%s)", amount, kind, ref, "funded" if funded else "still short")
    if funded:
        if kind == "deploy":
            service.activate(ref)
            return ref
        service.confirm_deposit(ref)
    return None


def mark_stale_claims() -> int:
    """A transfer left 'processing' (the process died) may or may not have been refunded. Never retry it blindly."""
    cutoff = now() - timedelta(seconds=STALE_CLAIM_S)
    with Session(engine) as s:
        rows = s.exec(select(ChainTransfer).where(ChainTransfer.disposition == "processing", ChainTransfer.created_at < cutoff)).all()
        for r in rows:
            r.disposition, r.note = "refund_unconfirmed", "process stopped mid-handling; check the chain before acting"
            s.add(r)
        s.commit()
        if rows:
            log.error("%d transfer(s) marked refund_unconfirmed: manual review needed", len(rows))
        return len(rows)


def unresolved_transfers() -> int:
    with Session(engine) as s:
        return len(s.exec(select(ChainTransfer).where(ChainTransfer.disposition.in_(("refund_failed", "refund_unconfirmed")))).all())


def watch_once() -> dict:
    """One tick: new finalized transfers, then activation retries, then expiries."""
    ensure_cursor()
    mark_stale_claims()
    funding = service.funding_address()
    with Session(engine) as s:
        cursor = s.get(WatcherCursor, CURSOR).last_signature
    handled, attempted = 0, set()
    for sig in sol.finalized_signatures_since(watch_address(), cursor):
        if (sid := process_signature(sig, funding)):  # an error stops the tick here and the cursor stays put: it is retried
            attempted.add(sid)
        _set_cursor(sig)
        handled += 1
    return {"transfers": handled, "activations": service.activate_ready(skip=attempted), "expired": service.expire_due(),
            "withdrawals": withdrawals.advance_pending()}


def register(sched) -> None:
    """Add the watcher to the app's scheduler."""
    def tick():
        try:
            out = watch_once()
            if any(out.values()):
                log.info("watcher: %s", out)
        except Exception:
            log.exception("watcher tick failed (will retry)")

    def snapshots():
        try:
            n = service.snapshot_all()
            log.debug("snapshots: %s", n)
        except Exception:
            log.exception("snapshot tick failed (will retry)")

    sched.add_job(tick, "interval", seconds=5, id="deposit-watcher", max_instances=1, coalesce=True, misfire_grace_time=5)
    sched.add_job(snapshots, "interval", seconds=60, id="pnl-snapshots", max_instances=1, coalesce=True, misfire_grace_time=30)
    from ..ai import samples

    samples.register(sched)  # the minute price sampler rides along with the watcher's jobs
    from ..ai import loop

    loop.register(sched)  # the agent cycle (only when AGENT_ENABLED)
