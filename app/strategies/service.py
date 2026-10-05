"""Delta-neutral hedge strategies: create (+ funding intent), top-ups, cancel, activation, refunds, serialisation.

Funding is by intent: the client registers an intent first (POST /strategies, POST /strategies/{id}/deposits) and then
sends a Solana transfer whose memo is exactly the intent id. This service is the only thing that confirms funding (see
watcher.py); the client never submits a signature.
"""
import logging
import time
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from .. import solana_client as sol
from ..config import settings
from ..db import engine
from ..errors import ServiceError
from ..models import (D_CANCELLED, D_CONFIRMED, D_EXPIRED, D_PENDING, S_ACTIVE, S_CANCELLED, S_CLOSING, S_EXPIRED, S_FAILED,
                      S_PENDING, S_REBALANCING, Action, Strategy, StrategyDeposit, now)
from ..state import state
from ..util import iso, num
from ..venue.base import account_lock
from . import attest as att

log = logging.getLogger("sereel.strategies")
LIVE = (S_ACTIVE, S_REBALANCING, S_CLOSING)  # statuses in which the strategy holds margin and a position
Q = Decimal("0.000001")


# ---- request bodies ---------------------------------------------------------

class CreateStrategyIn(BaseModel):
    fund_id: str
    fund_name: str | None = None
    market_id: str
    target_exposure_units: Decimal
    hedge_ratio_bps: int
    leverage: int
    rebalance_band_bps: int
    return_wallet_address: str | None = None
    registered_sender_address: str
    expected_amount_usd: Decimal


class DepositIn(BaseModel):
    amount_usd: Decimal
    registered_sender_address: str


def _bad(msg: str, code: str = "BAD_REQUEST") -> ServiceError:
    return ServiceError(code, msg, 400)


def venue():
    if state.venue is None:
        raise ServiceError("VENUE_NOT_CONFIGURED", "venue not initialised", 503)
    return state.venue


def market(market_id: str):
    try:
        return state.markets[market_id]
    except KeyError:
        raise _bad(f"unknown market '{market_id}'", "UNKNOWN_MARKET")


def funding_address() -> str:
    return str(sol.funding_kp().pubkey())


def target_size(st: Strategy) -> Decimal:
    return st.target_exposure_units * Decimal(st.hedge_ratio_bps) / 10_000


def required_margin(size_units: Decimal, price: Decimal, leverage: int) -> Decimal:
    """Notional / leverage, plus MARGIN_BUFFER_PCT of headroom."""
    return (size_units * price / leverage * (1 + settings.margin_buffer_pct / 100)).quantize(Q)


def ttl_seconds(multisig: bool) -> int:
    return settings.intent_ttl_multisig_seconds if multisig else settings.intent_ttl_seconds


def _check_wallet(addr: str, what: str) -> None:
    if not sol.is_valid_address(addr):
        raise _bad(f"{what} '{addr}' is not a valid Solana address")


def _owned(st: Strategy | None, org: str) -> Strategy:
    """404 for an unknown id, and for one that belongs to another org (never reveal which)."""
    if st is None or (org and st.org_id and st.org_id != org):
        raise ServiceError("NOT_FOUND", "strategy not found", 404)
    return st


# ---- create / cancel / top-up ----------------------------------------------

def create_strategy(body: CreateStrategyIn, user: str = "", org: str = "") -> Strategy:
    m = market(body.market_id)
    if body.target_exposure_units <= 0:
        raise _bad("target_exposure_units must be positive")
    if not 0 <= body.hedge_ratio_bps <= 10_000:
        raise _bad("hedge_ratio_bps must be between 0 and 10000")
    if not 0 <= body.rebalance_band_bps <= 10_000:
        raise _bad("rebalance_band_bps must be between 0 and 10000")
    cap = min(m.max_leverage, settings.max_leverage)
    if not 1 <= body.leverage <= cap:
        raise _bad(f"leverage must be between 1 and {cap} for {m.market_id}")
    if body.expected_amount_usd <= 0:
        raise _bad("expected_amount_usd must be positive")
    _check_wallet(body.registered_sender_address, "registered_sender_address")
    return_wallet = body.return_wallet_address or body.registered_sender_address
    _check_wallet(return_wallet, "return_wallet_address")

    size = body.target_exposure_units * Decimal(body.hedge_ratio_bps) / 10_000
    required = required_margin(size, venue().mark_price(body.market_id), body.leverage)
    floor = required * (1 - settings.rebalance_tolerance_pct / 100)
    if body.expected_amount_usd < floor:
        raise _bad(f"expected_amount_usd {body.expected_amount_usd} is below the required margin {required} "
                   f"(a {size} short at {body.leverage}x plus {settings.margin_buffer_pct}% buffer)")
    multisig = sol.is_multisig_address(body.registered_sender_address)  # a Squads vault is an off-curve PDA
    with Session(engine) as s:
        st = Strategy(fund_id=body.fund_id, fund_name=body.fund_name or body.fund_id, market_id=m.market_id,
                      market_symbol=m.symbol, hedge_ratio_bps=body.hedge_ratio_bps, leverage=body.leverage,
                      rebalance_band_bps=body.rebalance_band_bps, target_exposure_units=body.target_exposure_units,
                      return_wallet_address=return_wallet, owner_user_id=user, org_id=org,
                      registered_sender_address=body.registered_sender_address, multisig=multisig,
                      expected_amount_usd=body.expected_amount_usd, required_margin_usd=required,
                      expires_at=now() + timedelta(seconds=ttl_seconds(multisig)))
        s.add(st)
        s.add(Action(strategy_id=st.id, action="create", record={"intent_id": st.intent_id, "required_margin_usd": str(required)}))
        s.commit()
        s.refresh(st)
        return st


def get_strategy(sid: str, org: str = "") -> Strategy:
    with Session(engine) as s:
        return _owned(s.get(Strategy, sid), org)


def list_strategies(owner: str | None, fund_id: str | None, org: str = "") -> list[Strategy]:
    with Session(engine) as s:
        q = select(Strategy).order_by(Strategy.created_at.desc())
        if owner:
            q = q.where(Strategy.owner_user_id == owner)
        if fund_id:
            q = q.where(Strategy.fund_id == fund_id)
        if org:
            q = q.where((Strategy.org_id == org) | (Strategy.org_id == ""))
        return list(s.exec(q).all())


def cancel_strategy(sid: str, org: str = "") -> Strategy:
    """Only while the deploy intent is pending_funding. Anything already received is refunded to the registered sender."""
    with Session(engine) as s:
        st = _owned(s.get(Strategy, sid), org)
        if st.status != S_PENDING:
            raise ServiceError("CONFLICT", f"strategy is {st.status}; only a pending_funding intent can be cancelled", 409)
        st.status, st.updated_at = S_CANCELLED, now()
        s.add(st)
        s.commit()
        received = st.received_amount_usd or Decimal(0)
    if received > 0:
        refund_received(sid, "cancelled")
    return get_strategy(sid, org)


def create_deposit(sid: str, body: DepositIn, org: str = "") -> StrategyDeposit:
    if body.amount_usd <= 0:
        raise _bad("amount_usd must be positive")
    _check_wallet(body.registered_sender_address, "registered_sender_address")
    multisig = sol.is_multisig_address(body.registered_sender_address)
    with Session(engine) as s:
        st = _owned(s.get(Strategy, sid), org)
        if st.status != S_ACTIVE:
            raise ServiceError("CONFLICT", f"strategy is {st.status}; margin can only be added to an active strategy", 409)
        dep = StrategyDeposit(strategy_id=sid, amount_usd=body.amount_usd, expected_amount_usd=body.amount_usd,
                              registered_sender_address=body.registered_sender_address, multisig=multisig,
                              expires_at=now() + timedelta(seconds=ttl_seconds(multisig)))
        s.add(dep)
        s.commit()
        s.refresh(dep)
        return dep


def list_deposits(sid: str, org: str = "") -> list[StrategyDeposit]:
    with Session(engine) as s:
        _owned(s.get(Strategy, sid), org)
        return list(s.exec(select(StrategyDeposit).where(StrategyDeposit.strategy_id == sid)
                           .order_by(StrategyDeposit.created_at)).all())


def get_deposit(sid: str, did: str, org: str = "") -> StrategyDeposit:
    with Session(engine) as s:
        _owned(s.get(Strategy, sid), org)
        dep = s.get(StrategyDeposit, did)
        if not dep or dep.strategy_id != sid:
            raise ServiceError("NOT_FOUND", "deposit not found", 404)
        return dep


# ---- ledger helpers ---------------------------------------------------------

def sum_margins(s: Session, exclude: str | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(Strategy.margin_usd), 0)).where(Strategy.status.in_(LIVE))
    if exclude:
        q = q.where(Strategy.id != exclude)
    return Decimal(str(s.exec(q).one()))


def ledger_size(s: Session, market_id: str) -> Decimal:
    rows = s.exec(select(Strategy.size).where(Strategy.market_id == market_id, Strategy.status.in_(LIVE))).all()
    return sum((Decimal(str(r)) for r in rows), Decimal(0))


def ledger_drift(s: Session, market_id: str) -> Decimal:
    """Venue account position minus the sum of strategy sizes. Non-zero means a position the ledger does not explain
    (e.g. a crash between a fill and its commit), and trading on top of it would compound the error."""
    v = venue()
    quantum = Decimal(1).scaleb(-v.size_decimals(market_id))
    diff = v.position(None, market_id).size - ledger_size(s, market_id)
    return diff if abs(diff) >= quantum else Decimal(0)


def ledger_cash(s: Session) -> Decimal:
    """What the strategies are entitled to hold as cash on the venue: credited margin plus realized P&L and funding,
    minus fees (fees are paid out of the dex balance, so they reduce what is actually there)."""
    rows = s.exec(select(Strategy).where(Strategy.status.in_(LIVE))).all()
    return sum((r.margin_usd + r.realized_pnl_usd + r.funding_usd - r.fees_usd for r in rows), Decimal(0))


RECONCILE_TOLERANCE = Decimal("0.05")  # rounding and fee timing only; fees themselves are accounted for above


def reconcile() -> dict:
    """The strategies' ledger cash must not exceed the cash on the venue's dex balance."""
    with Session(engine) as s:
        margins, ledger = sum_margins(s), ledger_cash(s)
    first = next(iter(state.markets))
    pos = venue().position(None, first)
    cash = pos.account_value - pos.unrealized_pnl
    ok = ledger <= cash + RECONCILE_TOLERANCE
    if not ok:
        log.error("RECONCILIATION BREACH: strategy ledger cash %s exceeds dex cash %s", ledger, cash)
    return {"ok": ok, "sum_strategy_margin_usd": num(margins), "ledger_cash_usd": num(ledger), "dex_cash_usd": num(cash),
            "difference_usd": num(cash - ledger)}


# ---- refunds ----------------------------------------------------------------

def _record_action(strategy_id: str, action: str, record: dict, fund_id: str, solana_signature: str | None = None,
                   hl_order_ids: list | None = None) -> str | None:
    sig = att.attest(strategy_id, fund_id, action, record)
    with Session(engine) as s:
        s.add(Action(strategy_id=strategy_id, action=action, record=att.jsonable(record), solana_signature=solana_signature,
                     hl_order_ids=hl_order_ids or [], attestation_sig=sig))
        s.commit()
    return sig


def send_refund(to: str, amount: Decimal, ref: str) -> str:
    return sol.transfer_from(sol.funding_kp(), to, amount, memo=f"sereel refund {ref[:24]}")


def refund_received(sid: str, reason: str) -> None:
    """Return a strategy's received-but-unused funding to its registered sender and attest it."""
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        amount, sender, fund = st.received_amount_usd or Decimal(0), st.registered_sender_address, st.fund_id
    if amount <= 0:
        return
    try:
        sig = send_refund(sender, amount, sid)
    except Exception as e:
        log.error("REFUND FAILED for strategy %s (%s %s -> %s): %s", sid, amount, reason, sender, e)
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            st.failure_reason = f"REFUND FAILED ({reason}): {e}"
            s.add(st)
            s.commit()
        return
    _record_action(sid, "refund", {"reason": reason, "amount_usd": amount, "to": sender, "refund_signature": sig}, fund, sig)


# ---- activation -------------------------------------------------------------

def activate(sid: str) -> str:
    """Open the hedge for a fully funded strategy: margin, leverage, reduce-never short at exposure x ratio, attest.

    Returns active | retry | failed | skipped | drift. Transient venue/price errors are retried on later watcher ticks
    (up to MAX_ACTIVATION_ATTEMPTS); after that the strategy fails and the funding is refunded."""
    v = venue()
    with account_lock(v.account_key):
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            if not st or st.status != S_PENDING or (st.received_amount_usd or Decimal(0)) < st.expected_amount_usd:
                return "skipped"
            received, size = st.received_amount_usd, target_size(st)
            try:
                if ledger_drift(s, st.market_id):
                    log.error("activation of %s held: venue position differs from the ledger by %s", sid, ledger_drift(s, st.market_id))
                    return "drift"
                st.activation_attempts += 1
                s.add(st)
                s.commit()
                v.ensure_margin(st.market_id, sum_margins(s) + received)
                fill = v.set_position(st.id, st.market_id, -size, current_size=Decimal(0)) if size > 0 else None
            except ServiceError as e:
                if st.activation_attempts >= settings.max_activation_attempts or e.code == "UNKNOWN_MARKET":
                    outcome, reason = "failed", f"{e.code}: {e.message}"
                else:
                    st.failure_reason = f"{e.code}: {e.message} (retrying, attempt {st.activation_attempts})"
                    s.add(st)
                    s.commit()
                    log.warning("activation of %s will retry: %s", sid, st.failure_reason)
                    return "retry"
            else:
                st.size = fill.filled if fill else Decimal(0)
                st.entry_px = (fill.avg_px or Decimal(0)) if fill else Decimal(0)
                st.fees_usd = fill.fee if fill else Decimal(0)
                st.hl_order_ids = list(fill.oids) if fill else []
                st.margin_usd, st.status, st.failure_reason = received, S_ACTIVE, None
                st.deployed_at = st.updated_at = now()
                s.add(st)
                s.commit()
                record = {"event": "deploy", "target_size": size, "filled": st.size, "remaining": fill.remaining if fill else 0,
                          "entry_px": st.entry_px, "fee": st.fees_usd, "margin_usd": received, "leverage": st.leverage,
                          "market": state.markets[st.market_id].hl_coin, "mark": v.mark_price(st.market_id),
                          "funding_signature": st.deploy_signature, "hl_oids": st.hl_order_ids,
                          "market_closed": v.market_closed.get(st.market_id, False)}
                fund, oids = st.fund_id, list(st.hl_order_ids)
                outcome = "active"
    if outcome == "active":
        asig = _record_action(sid, "deploy", record, fund, hl_order_ids=oids)
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            st.last_attestation_sig = asig
            s.add(st)
            s.commit()
        return "active"
    _fail_and_refund(sid, reason)
    return "failed"


def _fail_and_refund(sid: str, reason: str) -> None:
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.status, st.failure_reason, st.updated_at = S_FAILED, reason, now()
        s.add(st)
        s.commit()
        fund = st.fund_id
    log.error("strategy %s failed: %s", sid, reason)
    _record_action(sid, "deploy_failed", {"reason": reason}, fund)
    refund_received(sid, "deploy failed")


def activate_ready() -> int:
    """Retry activation for every fully funded strategy that is still pending (the first attempt may have hit a
    transient error). Returns how many were attempted."""
    with Session(engine) as s:
        ids = [st.id for st in s.exec(select(Strategy).where(Strategy.status == S_PENDING)).all()
               if (st.received_amount_usd or Decimal(0)) >= st.expected_amount_usd]
    for sid in ids:
        activate(sid)
    return len(ids)


def confirm_deposit(did: str) -> None:
    """A top-up intent is fully funded: credit it to the strategy's margin, attest."""
    v = venue()
    with account_lock(v.account_key):
        with Session(engine) as s:
            dep = s.get(StrategyDeposit, did)
            if not dep or dep.status != D_PENDING:
                return
            st = s.get(Strategy, dep.strategy_id)
            amount = dep.received_amount_usd
            if st.status not in LIVE:  # closed while the money was on its way: give it back
                dep.status = D_CANCELLED
                s.add(dep)
                s.commit()
                refund_deposit = True
            else:
                refund_deposit = False
                try:
                    v.ensure_margin(st.market_id, sum_margins(s) + amount)
                except ServiceError as e:  # the funds ARE ours now; record the credit and let reconciliation flag the gap
                    log.error("top-up %s credited but the venue margin could not be raised: %s %s", did, e.code, e.message)
                st.margin_usd += amount
                st.updated_at = now()
                dep.status = D_CONFIRMED
                s.add(st)
                s.add(dep)
                s.commit()
                record = {"event": "deposit", "amount_usd": amount, "margin_usd": st.margin_usd, "signature": dep.solana_signature}
                fund, sid = st.fund_id, st.id
    if refund_deposit:
        with Session(engine) as s:
            dep = s.get(StrategyDeposit, did)
            to, amount, sig = dep.registered_sender_address, dep.received_amount_usd, dep.solana_signature
            sid = dep.strategy_id
            fund = s.get(Strategy, sid).fund_id
        try:
            rsig = send_refund(to, amount, did)
            _record_action(sid, "refund", {"reason": "strategy not active", "amount_usd": amount, "to": to}, fund, rsig)
        except Exception as e:
            log.error("REFUND FAILED for deposit %s: %s", did, e)
        return
    asig = _record_action(sid, "deposit", record, fund, dep_signature(did))
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.last_attestation_sig = asig
        s.add(st)
        s.commit()


def dep_signature(did: str) -> str | None:
    with Session(engine) as s:
        return s.get(StrategyDeposit, did).solana_signature


# ---- expiry -----------------------------------------------------------------

def expire_due() -> int:
    """pending_funding -> expired once the window passes unconfirmed. Partial funding is refunded."""
    t = now()
    n = 0
    with Session(engine) as s:
        strategies = [x.id for x in s.exec(select(Strategy).where(Strategy.status == S_PENDING, Strategy.expires_at < t)).all()
                      if (x.received_amount_usd or Decimal(0)) < x.expected_amount_usd]  # a fully funded one is activating
        deposits = [x.id for x in s.exec(select(StrategyDeposit).where(StrategyDeposit.status == D_PENDING,
                                                                      StrategyDeposit.expires_at < t)).all()]
    for sid in strategies:
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            st.status, st.updated_at = S_EXPIRED, now()
            s.add(st)
            s.commit()
        refund_received(sid, "expired")
        n += 1
    for did in deposits:
        with Session(engine) as s:
            dep = s.get(StrategyDeposit, did)
            dep.status = D_EXPIRED
            s.add(dep)
            s.commit()
            to, got, sid = dep.registered_sender_address, dep.received_amount_usd or Decimal(0), dep.strategy_id
            fund = s.get(Strategy, sid).fund_id
        if got > 0:
            try:
                _record_action(sid, "refund", {"reason": "top-up expired", "amount_usd": got, "to": to}, fund, send_refund(to, got, did))
            except Exception as e:
                log.error("REFUND FAILED for expired deposit %s: %s", did, e)
        n += 1
    return n


# ---- serialisation (Cantina v4 shapes) -------------------------------------

_mark_cache: dict[str, tuple[float, Decimal]] = {}
_liq_cache: dict[str, tuple[float, Decimal | None]] = {}


def mark_for(market_id: str) -> Decimal | None:
    """Live mark with a 2s cache, so a list of strategies does not hit the venue once per row. None if the venue is down."""
    hit = _mark_cache.get(market_id)
    if hit and time.time() - hit[0] < 2:
        return hit[1]
    try:
        mark = venue().mark_price(market_id)
    except Exception as e:
        log.warning("mark unavailable for %s: %s", market_id, e)
        return None
    _mark_cache[market_id] = (time.time(), mark)
    return mark


def venue_liquidation_for(market_id: str) -> Decimal | None:
    """The venue's own liquidation price for the shared account position (2s cache); None if flat or unavailable."""
    hit = _liq_cache.get(market_id)
    if hit and time.time() - hit[0] < 2:
        return hit[1]
    try:
        liq = venue().position(None, market_id).liquidation_px
    except Exception as e:
        log.warning("venue liquidation price unavailable for %s: %s", market_id, e)
        liq = None
    _liq_cache[market_id] = (time.time(), liq)
    return liq


def position_out(st: Strategy, mark: Decimal | None) -> dict | None:
    """The live position of this strategy's share of the shared account. None until the hedge is open."""
    if st.status not in LIVE or mark is None:
        return None
    size = st.size
    magnitude = abs(size)
    unrealized = (mark - st.entry_px) * size
    cash = st.margin_usd + st.realized_pnl_usd + st.funding_usd - st.fees_usd
    equity = cash + unrealized
    rate = venue().maintenance_rate(st.market_id)
    maintenance = magnitude * mark * rate
    health = max(0, min(10_000, int(10_000 * (equity - maintenance) / equity))) if equity > 0 else 0
    liq = None
    if magnitude:
        liq = (cash + st.entry_px * magnitude) / (magnitude * (1 + rate)) if size < 0 \
            else (st.entry_px * magnitude - cash) / (magnitude * (1 - rate))
    venue_liq = venue_liquidation_for(st.market_id) if magnitude else None
    if liq is not None and venue_liq is not None:
        # the formula above assumes all of the strategy's margin backs the position; the venue's isolated margin can be
        # smaller. Report the nearer of the two, so the figure is never rosier than the venue's own.
        liq = min(liq, venue_liq) if size < 0 else max(liq, venue_liq)
    return {"side": "short" if size < 0 else "long" if size > 0 else "flat", "size_units": num(magnitude),
            "entry_price_usd": num(st.entry_px), "mark_price_usd": num(mark), "margin_usd": num(st.margin_usd),
            "unrealized_pnl_usd": num(unrealized), "realized_pnl_usd": num(st.realized_pnl_usd),
            "funding_paid_usd": num(st.funding_usd), "fees_usd": num(st.fees_usd), "margin_health_bps": health,
            "maintenance_margin_usd": num(maintenance), "liquidation_price_usd": num(liq),
            "hl_order_ids": list(st.hl_order_ids or [])}


def strategy_out(st: Strategy) -> dict:
    mark = mark_for(st.market_id)
    size = target_size(st)
    received = st.received_amount_usd
    shortfall = max(st.expected_amount_usd - received, Decimal(0)) if received is not None and st.status == S_PENDING else None
    return {
        "id": st.id, "template": st.template, "status": st.status, "fund_id": st.fund_id, "fund_name": st.fund_name,
        "market_id": st.market_id, "market_symbol": st.market_symbol, "hedge_ratio_bps": st.hedge_ratio_bps,
        "leverage": st.leverage, "rebalance_band_bps": st.rebalance_band_bps,
        "target_exposure_units": num(st.target_exposure_units), "target_hedge_size_units": num(size),
        "target_hedge_size_usd": num(size * mark) if mark is not None else None,
        "required_margin_usd": num(st.required_margin_usd), "return_wallet_address": st.return_wallet_address,
        "funding_address": funding_address(), "intent_id": st.intent_id,
        "registered_sender_address": st.registered_sender_address, "expected_amount_usd": num(st.expected_amount_usd),
        "received_amount_usd": num(received), "shortfall_usd": num(shortfall), "expires_at": iso(st.expires_at),
        "owner_user_id": st.owner_user_id, "created_at": iso(st.created_at), "updated_at": iso(st.updated_at),
        "deployed_at": iso(st.deployed_at), "closed_at": iso(st.closed_at), "position": position_out(st, mark),
        "market_closed": bool(venue().market_closed.get(st.market_id, False)), "failure_reason": st.failure_reason,
    }


def deposit_out(d: StrategyDeposit) -> dict:
    received = d.received_amount_usd
    shortfall = max(d.expected_amount_usd - received, Decimal(0)) if received is not None and d.status == D_PENDING else None
    return {"id": d.id, "strategy_id": d.strategy_id, "intent_id": d.intent_id, "amount_usd": num(d.amount_usd),
            "expected_amount_usd": num(d.expected_amount_usd), "received_amount_usd": num(received),
            "shortfall_usd": num(shortfall), "source_wallet_address": d.source_wallet_address,
            "registered_sender_address": d.registered_sender_address, "status": d.status, "expires_at": iso(d.expires_at),
            "solana_signature": d.solana_signature, "created_at": iso(d.created_at)}
