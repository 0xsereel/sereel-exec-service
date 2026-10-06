"""Money leaving a strategy: return-excess (keeps the hedge open) and close (closes it). One state machine:

    requested -> position_closed -> released -> bridging -> completed          (failed from any of the first four)

The API call returns `requested` at once and a background step machine does the work (the frontend polls the
withdrawal every 5s). Steps are idempotent and persisted, so a restart resumes where it stopped:

  requested        close: check book depth, then close with reduce-only IOCs; excess: nothing to close
  position_closed  move the USDC from the market's dex balance back to the main balance
  released         bridge to Solana (testnet: MirroredRoute, nothing moves; production: CCTP, a stub)
  bridging         pay the destination on Solana from the funding wallet (devnet: minting if short), attest

Failure handling: a failed close BEFORE anything was traded puts the strategy back to `active`; release and bridge retry a
few times on transient errors; the Solana payout is NEVER retried automatically (a timeout may still have landed), it
fails with needs_operator and `sereel strategies retry-withdrawal` re-runs it after a person has checked the chain.
"""
import logging
import threading
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .. import solana_client as sol
from ..db import engine
from ..errors import ServiceError
from ..models import (S_ACTIVE, S_CLOSED, S_CLOSING, W_BRIDGING, W_COMPLETED, W_FAILED, W_POSITION_CLOSED, W_RELEASED,
                      W_REQUESTED, Action, Strategy, Withdrawal, now)
from ..util import iso, num
from ..venue.base import MirroredRoute, account_lock
from . import attest as att
from . import service as svc

log = logging.getLogger("sereel.withdrawals")
TERMINAL = (W_COMPLETED, W_FAILED)
MAX_STEP_ATTEMPTS = 5  # release / bridge on transient errors
EXCESS_FACTOR = Decimal("1.5")  # equity left after returning excess must stay >= 1.5 x the required margin
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


class _Stop(Exception):
    """A step decided the withdrawal fails now (no retry)."""

    def __init__(self, reason: str, operator: bool = False):
        super().__init__(reason)
        self.reason, self.operator = reason, operator


# ---- shapes -----------------------------------------------------------------

def withdrawal_out(w: Withdrawal) -> dict:
    return {"id": w.id, "strategy_id": w.strategy_id, "type": w.type, "amount_usd": num(w.amount_usd),
            "destination_wallet_address": w.destination_wallet_address, "status": w.status, "failure_reason": w.failure_reason,
            "solana_signature": w.solana_signature,
            "attestation_url": sol.explorer_url(w.attestation_sig) if w.attestation_sig else None,
            "created_at": iso(w.created_at), "updated_at": iso(w.updated_at)}


def _load(wid: str) -> Withdrawal | None:
    with Session(engine) as s:
        return s.get(Withdrawal, wid)


def _update(wid: str, **fields) -> Withdrawal:
    refs = fields.pop("refs", None)
    with Session(engine) as s:
        w = s.get(Withdrawal, wid)
        for k, v in fields.items():
            setattr(w, k, v)
        if refs:
            w.refs = {**(w.refs or {}), **refs}  # a new dict, so the JSON column is seen as changed
        w.updated_at = now()
        s.add(w)
        s.commit()
        s.refresh(w)
        return w


def get_withdrawal(sid: str, wid: str, org: str = "") -> Withdrawal:
    svc.get_strategy(sid, org)
    w = _load(wid)
    if w is None or w.strategy_id != sid:
        raise ServiceError("NOT_FOUND", "withdrawal not found", 404)
    return w


def list_withdrawals(sid: str, org: str = "") -> list[Withdrawal]:
    svc.get_strategy(sid, org)
    with Session(engine) as s:
        return list(s.exec(select(Withdrawal).where(Withdrawal.strategy_id == sid).order_by(Withdrawal.created_at)).all())


# ---- requests -----------------------------------------------------------------

def _existing(sid: str, authorization, wtype: str, dest: str, amount: Decimal | None) -> Withdrawal | None:
    """A retried request (same signed authorization, same content) returns the withdrawal it already created."""
    nonce = authorization.get("nonce") if isinstance(authorization, dict) else None
    if not isinstance(nonce, str):
        return None
    with Session(engine) as s:
        w = s.exec(select(Withdrawal).where(Withdrawal.auth_nonce == nonce)).first()
    if w and w.strategy_id == sid and w.type == wtype and w.destination_wallet_address == dest and \
            (amount is None or w.amount_usd == amount):
        return w
    return None


def _nonce_for(authorization) -> str:
    nonce = authorization.get("nonce") if isinstance(authorization, dict) else None
    return nonce if isinstance(nonce, str) and nonce else f"unsigned-{now().timestamp()}-{id(authorization)}"  # dev bypass


def _inflight(sid: str) -> Withdrawal | None:
    with Session(engine) as s:
        return s.exec(select(Withdrawal).where(Withdrawal.strategy_id == sid, Withdrawal.status.not_in(TERMINAL))).first()


def _require_liquidity(st: Strategy, v) -> None:
    """Closing buys back a short (sells a long): the shared NO_LIQUIDITY rule, for the position's whole size."""
    svc.require_liquidity(st.market_id, st.size < 0, abs(st.size), "closing")


def _equity(st: Strategy, mark: Decimal) -> tuple[Decimal, Decimal]:
    """(cash the strategy holds, cash + unrealized at `mark`)."""
    cash = st.margin_usd + st.realized_pnl_usd + st.funding_usd - st.fees_usd
    return cash, cash + ((mark - st.entry_px) * st.size if st.size else Decimal(0))


def request_close(sid: str, params: dict, authorization, org: str = "") -> Withdrawal:
    dest = params.get("destination_wallet_address")
    if not dest:
        raise svc._bad("destination_wallet_address is required")
    svc._check_wallet(dest, "destination_wallet_address")
    st = svc.get_strategy(sid, org)
    if prior := _existing(sid, authorization, "close", dest, None):
        return prior
    if live := _inflight(sid):
        raise ServiceError("CONFLICT", f"withdrawal {live.id} is already {live.status}", 409)
    if st.status == S_CLOSING:
        with Session(engine) as s:
            stuck = s.exec(select(Withdrawal).where(Withdrawal.strategy_id == sid, Withdrawal.type == "close",
                                                    Withdrawal.status == W_FAILED)).all()
        if any((w.refs or {}).get("released_usd") is not None for w in stuck):
            raise ServiceError("CONFLICT", "a previous close released the funds but did not complete; an operator must run "
                               "`sereel strategies retry-withdrawal` (closing again would pay out twice)", 409)
    elif st.status != S_ACTIVE:
        raise ServiceError("CONFLICT", f"strategy is {st.status}; only an active strategy can be closed", 409)
    v = svc.venue()
    if st.size != 0:
        _require_liquidity(st, v)  # before the signature is spent, and before anything is sent
    st, signer = svc.authorize_action(sid, "close_strategy", authorization, params, org)
    _, equity = _equity(st, v.mark_price(st.market_id))
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.status, st.updated_at = S_CLOSING, now()
        w = Withdrawal(strategy_id=sid, type="close", amount_usd=max(equity, Decimal(0)), destination_wallet_address=dest,
                       auth_nonce=_nonce_for(authorization), refs={"signed_by": signer, "authorization": authorization,
                                                                   "estimate": True})
        s.add(st)
        s.add(w)
        try:
            s.commit()
        except IntegrityError:  # the same signed request arrived twice at once
            s.rollback()
            return _existing(sid, authorization, "close", dest, None) or w
        s.refresh(w)
        return w


def request_excess(sid: str, params: dict, authorization, org: str = "") -> Withdrawal:
    try:
        amount, dest = Decimal(params["amount_usd"]), params["destination_wallet_address"]
    except (KeyError, ArithmeticError):
        raise svc._bad("amount_usd and destination_wallet_address are required")
    if amount <= 0:
        raise svc._bad("amount_usd must be positive")
    svc._check_wallet(dest, "destination_wallet_address")
    st = svc.get_strategy(sid, org)
    if prior := _existing(sid, authorization, "return_excess", dest, amount):
        return prior
    if st.status != S_ACTIVE:
        raise ServiceError("CONFLICT", f"strategy is {st.status}; excess can only be returned while it is active", 409)
    v = svc.venue()
    mark = v.mark_price(st.market_id)

    def cap_for(s_: Strategy) -> Decimal:
        _, equity = _equity(s_, mark)
        required = max(s_.required_margin_usd, svc.required_margin(svc.target_size(s_), mark, s_.leverage))
        return min(s_.margin_usd, equity - required * EXCESS_FACTOR)

    if amount > cap_for(st):
        raise ServiceError("WITHDRAW_BELOW_MARGIN", f"at most {max(cap_for(st), Decimal(0)):.2f} USD can be returned: the equity left "
                           f"must stay at least {EXCESS_FACTOR}x the required margin", 400)
    st, signer = svc.authorize_action(sid, "return_excess", authorization, params, org)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        if amount > cap_for(st):  # re-check under the write: another withdrawal may have been taken meanwhile
            raise ServiceError("WITHDRAW_BELOW_MARGIN", "another withdrawal reduced what can be returned; try a smaller amount", 400)
        st.margin_usd -= amount  # reserved now; restored if the withdrawal fails before the funds leave the venue
        st.updated_at = now()
        w = Withdrawal(strategy_id=sid, type="return_excess", amount_usd=amount, destination_wallet_address=dest,
                       auth_nonce=_nonce_for(authorization), refs={"signed_by": signer, "authorization": authorization})
        s.add(st)
        s.add(w)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            return _existing(sid, authorization, "return_excess", dest, amount) or w
        s.refresh(w)
        return w


# ---- the state machine ---------------------------------------------------------

def _lock_for(wid: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(wid, threading.Lock())


def advance_withdrawal(wid: str) -> str:
    """Run a withdrawal as far as it will go. Safe to call from several places: one runner per withdrawal."""
    lock = _lock_for(wid)
    if not lock.acquire(blocking=False):
        return "busy"
    try:
        for _ in range(8):
            w = _load(wid)
            if w is None or w.status in TERMINAL:
                return w.status if w else "gone"
            before = w.status
            step = {W_REQUESTED: _close_step, W_POSITION_CLOSED: _release_step, W_RELEASED: _bridge_step,
                    W_BRIDGING: _payout_step}[before]
            try:
                step(wid)
            except _Stop as s:
                _fail(wid, s.reason, s.operator)
                return W_FAILED
            except Exception as e:  # noqa: BLE001
                if before in (W_POSITION_CLOSED, W_RELEASED) and _bump_attempts(wid) < MAX_STEP_ATTEMPTS:
                    log.warning("withdrawal %s step %s failed, will retry: %s", wid, before, e)
                    return "retry"
                _fail(wid, f"{before}: {getattr(e, 'message', e)}", operator=True)
                return W_FAILED
        return _load(wid).status
    finally:
        lock.release()


def _bump_attempts(wid: str) -> int:
    n = (_load(wid).refs or {}).get("attempts", 0) + 1
    _update(wid, refs={"attempts": n})
    return n


def advance_pending() -> int:
    """The watcher tick: pick up withdrawals that are not terminal (retries, and anything left by a restart)."""
    with Session(engine) as s:
        ids = [w.id for w in s.exec(select(Withdrawal).where(Withdrawal.status.not_in(TERMINAL))).all()]
    for wid in ids:
        advance_withdrawal(wid)
    return len(ids)


def _close_step(wid: str) -> None:
    w = _load(wid)
    if w.type == "return_excess":
        _update(wid, status=W_POSITION_CLOSED)  # nothing to close: the hedge stays open
        return
    v = svc.venue()
    sid = w.strategy_id
    with account_lock(v.account_key):
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            if st.size != 0:
                try:
                    _require_liquidity(st, v)  # again, now: the book may have changed since the request
                except ServiceError as e:
                    raise _Stop(f"{e.code}: {e.message}")
                if svc.ledger_drift(s, st.market_id):
                    raise _Stop("the venue position differs from the ledger; closing is held until it is reconciled", True)
        svc.accrue_funding(sid)  # before the size changes
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            if st.size != 0:
                mark = v.mark_price(st.market_id)
                try:
                    fill = v.set_position(sid, st.market_id, Decimal(0), current_size=st.size)  # reduce-only IOCs with retries
                except ServiceError as e:
                    raise _Stop(f"{e.code}: {e.message}")
                realized = svc.apply_fill(st, fill.filled, fill.avg_px or mark, fill.fee)
                st.hl_order_ids = list(st.hl_order_ids or []) + list(fill.oids)
                s.add(st)
                s.commit()
                _update(wid, refs={"traded": True, "close": {"filled": str(fill.filled), "remaining": str(fill.remaining),
                                                              "avg_px": str(fill.avg_px), "fee": str(fill.fee),
                                                              "realized_pnl_usd": str(realized), "oids": list(fill.oids)}})
                if st.size != 0:
                    raise _Stop(f"partially closed: {abs(st.size)} is still open (filled {abs(fill.filled)}); close again once "
                                "liquidity returns")
            cash = st.margin_usd + st.realized_pnl_usd + st.funding_usd - st.fees_usd
            final = {"margin_usd": str(st.margin_usd), "realized_pnl_usd": str(st.realized_pnl_usd),
                     "funding_usd": str(st.funding_usd), "fees_usd": str(st.fees_usd)}
    _update(wid, amount_usd=max(cash, Decimal(0)), status=W_POSITION_CLOSED, refs={"final": final, "estimate": False})
    svc.take_snapshot(sid, "close_position")


def _release_step(wid: str) -> None:
    w = _load(wid)
    v = svc.venue()
    market_id = svc.get_strategy(w.strategy_id).market_id
    if w.amount_usd > 0:
        v.release_margin(market_id, w.amount_usd)  # dex balance -> main balance (an error here is retried)
    if w.type == "close":
        with Session(engine) as s:
            st = s.get(Strategy, w.strategy_id)
            st.margin_usd = Decimal(0)  # the money has left the strategy's venue balance
            s.add(st)
            s.commit()
    _update(wid, status=W_RELEASED, refs={"released_usd": str(w.amount_usd)})


def _bridge_step(wid: str) -> None:
    w = _load(wid)
    route = MirroredRoute().from_venue(w.amount_usd, w.destination_wallet_address, w.id)  # production: CctpHyperliquidRoute
    _update(wid, status=W_BRIDGING, refs={"route": route})


def _payout_step(wid: str) -> None:
    w = _load(wid)
    if w.solana_signature:  # the money was already sent (a later step failed): only finish, NEVER send again
        _complete(wid, w.solana_signature)
        return
    if w.amount_usd > 0 and (w.refs or {}).get("payout_started"):
        raise _Stop("the payout was started by an earlier attempt and its result is unknown; check the funding wallet's "
                    f"transactions for the memo 'sereel {w.type} {w.id[:8]}' before retrying", True)
    _update(wid, refs={"payout_started": True})
    sig = None
    if w.amount_usd > 0:
        try:
            sig = sol.pay(w.destination_wallet_address, w.amount_usd, f"sereel {w.type} {w.id[:8]}", source=sol.funding_kp())
        except Exception as e:  # noqa: BLE001  (a timeout may still have landed: never retried automatically)
            raise _Stop(f"payout failed or its result is unknown: {e}", True)
        _update(wid, solana_signature=sig)  # recorded the moment it is known, before anything else can fail
    _complete(wid, sig)


def _complete(wid: str, sig: str | None) -> None:
    w = _update(wid, solana_signature=sig)
    with Session(engine) as s:
        st = s.get(Strategy, w.strategy_id)
        if w.type == "close":
            st.status, st.closed_at, st.size, st.entry_px, st.margin_usd = S_CLOSED, now(), Decimal(0), Decimal(0), Decimal(0)
        st.updated_at = now()
        s.add(st)
        s.commit()
        fund = st.fund_id
    refs = w.refs or {}
    record = {"event": w.type, "withdrawal_id": w.id, "amount_usd": w.amount_usd, "destination": w.destination_wallet_address,
              "signed_by": refs.get("signed_by"), "authorization_nonce": (refs.get("authorization") or {}).get("nonce"),
              "close": refs.get("close"), "final": refs.get("final"), "released_usd": refs.get("released_usd"),
              "route": refs.get("route"), "solana_signature": sig}
    asig = svc._record_action(w.strategy_id, w.type, record, fund, sig, (refs.get("close") or {}).get("oids", []),
                              signed_by=refs.get("signed_by"), authorization=refs.get("authorization"))
    _update(wid, status=W_COMPLETED, attestation_sig=asig)
    with Session(engine) as s:
        st = s.get(Strategy, w.strategy_id)
        st.last_attestation_sig = asig
        s.add(st)
        s.commit()
    svc.take_snapshot(w.strategy_id, "close" if w.type == "close" else "return_excess")


def _fail(wid: str, reason: str, operator: bool = False) -> None:
    w = _load(wid)
    refs = w.refs or {}
    failed_at = w.status
    log.error("withdrawal %s failed at %s: %s", wid, failed_at, reason)
    with Session(engine) as s:
        st = s.get(Strategy, w.strategy_id)
        if w.type == "close" and not refs.get("traded") and failed_at == W_REQUESTED and st.status == S_CLOSING:
            st.status = S_ACTIVE  # nothing happened on the venue: the strategy goes on as before
        if w.type == "return_excess" and refs.get("released_usd") is None and not refs.get("margin_restored"):
            st.margin_usd += w.amount_usd  # the funds never left the venue: undo the reservation
            refs = {**refs, "margin_restored": True}
        st.updated_at = now()
        s.add(st)
        s.commit()
        fund = st.fund_id
    _update(wid, status=W_FAILED, failure_reason=reason,
            refs={"failed_at": failed_at, "needs_operator": operator, "margin_restored": refs.get("margin_restored", False)})
    svc._record_action(w.strategy_id, "withdrawal_failed", {"withdrawal_id": wid, "type": w.type, "failed_at": failed_at,
                                                             "reason": reason}, fund,
                       signed_by=refs.get("signed_by"), attest=True)


def unresolved() -> int:
    with Session(engine) as s:
        return len([w for w in s.exec(select(Withdrawal).where(Withdrawal.status == W_FAILED)).all()
                    if (w.refs or {}).get("needs_operator")])


def retry_withdrawal(wid: str, confirm_not_sent: bool = False) -> str:
    """OPERATOR ONLY (CLI). Resume a failed withdrawal from the step it failed at. A payout whose result is unknown is only
    retried with confirm_not_sent=True, after a person has checked that nothing was sent."""
    w = _load(wid)
    if w is None:
        raise ServiceError("NOT_FOUND", "withdrawal not found", 404)
    if w.status != W_FAILED:
        raise ServiceError("CONFLICT", f"withdrawal is {w.status}, not failed", 409)
    refs = w.refs or {}
    at = refs.get("failed_at", W_REQUESTED)
    if at == W_BRIDGING and refs.get("payout_started") and not w.solana_signature and not confirm_not_sent:
        raise ServiceError("CONFLICT", "the payout may already have been sent; check the chain, then retry with --confirm-not-sent", 409)
    with Session(engine) as s:
        st = s.get(Strategy, w.strategy_id)
        if w.type == "close" and st.status == S_ACTIVE:
            st.status = S_CLOSING
        if w.type == "return_excess" and refs.get("margin_restored"):
            st.margin_usd -= w.amount_usd  # reserve it again
        s.add(st)
        s.commit()
    _update(wid, status=at, failure_reason=None, refs={"attempts": 0, "needs_operator": False, "margin_restored": False,
                                                       "payout_started": False if confirm_not_sent else refs.get("payout_started")})
    return advance_withdrawal(wid)
