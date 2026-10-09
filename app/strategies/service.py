"""Delta-neutral hedge strategies: create (+ funding intent), top-ups, cancel, activation, refunds, serialisation.

Funding is by intent: the client registers an intent first (POST /strategies, POST /strategies/{id}/deposits) and then
sends a Solana transfer whose memo is exactly the intent id. This service is the only thing that confirms funding (see
watcher.py); the client never submits a signature.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from .. import auth
from .. import solana_client as sol
from ..config import settings
from ..db import engine
from ..errors import ServiceError
from ..models import (D_CANCELLED, D_CONFIRMED, D_EXPIRED, D_PENDING, S_ACTIVE, S_CANCELLED, S_CLOSING, S_EXPIRED, S_FAILED,
                      S_PENDING, S_REBALANCING, Action, PnlSnapshot, Strategy, StrategyDeposit, now)
from ..state import state
from ..util import iso, num
from ..venue.base import DEFAULT_SLIPPAGE, account_lock
from . import attest as att
from .. import delegates
from ..ai import decisions

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
    owner_pubkey: str | None = None  # who may manage the strategy: the manager's Sereel wallet ...
    owner_multisig: str | None = None  # ... or a Squads multisig account (its current members). Exactly one.


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
        m = state.markets[market_id]
    except KeyError:
        raise _bad(f"unknown market '{market_id}'", "UNKNOWN_MARKET")
    if not m.enabled:  # the same switch that lists it as "coming_soon": the API must not accept what the UI disables
        raise _bad(f"market '{market_id}' is {m.status}: new strategies cannot be created on it yet")
    return m


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


def validate_owner(pubkey: str | None, multisig: str | None, required: bool = True) -> tuple[str | None, str | None]:
    """Exactly one owner; a multisig must really be a Squads multisig account (read on-chain, not cached)."""
    if pubkey and multisig:
        raise _bad("send either owner_pubkey or owner_multisig, not both")
    if not pubkey and not multisig:
        if required:
            raise _bad("owner_pubkey or owner_multisig is required: it decides who may sign actions on this strategy")
        return None, None
    if pubkey:
        _check_wallet(pubkey, "owner_pubkey")
    else:
        _check_wallet(multisig, "owner_multisig")
        auth.squads_members(multisig, fresh=True)  # BAD_REQUEST unless it is a Squads multisig account
    return pubkey, multisig


def _owned(st: Strategy | None, org: str) -> Strategy:
    """404 for an unknown id, and for one that belongs to another org (never reveal which)."""
    if st is None or (org and st.org_id and st.org_id != org):
        raise ServiceError("NOT_FOUND", "strategy not found", 404)
    return st


# ---- create / cancel / top-up ----------------------------------------------

def quote_strategy(m, exposure: Decimal, hedge_ratio_bps: int, leverage: int,
                   expected_amount_usd: Decimal | None = None) -> tuple[Decimal, Decimal, Decimal]:
    """(hedge size, mark, required margin) for a strategy, applying the venue's minimum order and, when an amount is given, the
    margin floor. One function for create and for the setup assistant's draft, so a draft can never be a strategy that create
    would refuse."""
    size = exposure * Decimal(hedge_ratio_bps) / 10_000
    mark = venue().mark_price(m.market_id)
    notional, minimum = size * mark, settings.min_order_usd * Decimal("1.05")  # 5% cushion for the mark moving before the order
    if 0 < size and notional < minimum:
        need = (minimum / mark / (Decimal(hedge_ratio_bps) / 10_000)).quantize(Decimal("0.0001"), rounding="ROUND_UP")
        unit = m.unit
        raise _bad(f"the hedge would be {size} {unit} (about ${notional:.2f}), below the venue's ${settings.min_order_usd} minimum order. "
                   f"Raise target_exposure_units to at least {need.normalize():f} {unit} at hedge_ratio_bps {hedge_ratio_bps} "
                   f"(about ${required_margin(need * Decimal(hedge_ratio_bps) / 10_000, mark, leverage):.2f} of margin)")
    required = required_margin(size, mark, leverage)
    floor = required * (1 - settings.rebalance_tolerance_pct / 100)
    if expected_amount_usd is not None and expected_amount_usd < floor:
        raise _bad(f"expected_amount_usd {expected_amount_usd} is below the required margin {required} "
                   f"(a {size} short at {leverage}x plus {settings.margin_buffer_pct}% buffer)")
    return size, mark, required


def short_liquidation_price(cash: Decimal, entry: Decimal, size: Decimal, rate: Decimal) -> Decimal:
    """Price at which a short of `size` opened at `entry`, backed by `cash`, reaches maintenance (the position_out formula)."""
    return (cash + entry * size) / (size * (1 + rate))



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
    owner_pubkey, owner_multisig = validate_owner(body.owner_pubkey, body.owner_multisig,
                                                  required=not settings.auth_bypass_active)

    size, mark, required = quote_strategy(m, body.target_exposure_units, body.hedge_ratio_bps, body.leverage, body.expected_amount_usd)
    multisig = sol.is_multisig_address(body.registered_sender_address)  # a Squads vault is an off-curve PDA
    with Session(engine) as s:
        st = Strategy(fund_id=body.fund_id, fund_name=body.fund_name or body.fund_id, market_id=m.market_id,
                      market_symbol=m.symbol, hedge_ratio_bps=body.hedge_ratio_bps, leverage=body.leverage,
                      rebalance_band_bps=body.rebalance_band_bps, target_exposure_units=body.target_exposure_units,
                      return_wallet_address=return_wallet, owner_user_id=user, org_id=org,
                      owner_pubkey=owner_pubkey, owner_multisig=owner_multisig,
                      registered_sender_address=body.registered_sender_address, multisig=multisig,
                      expected_amount_usd=body.expected_amount_usd, required_margin_usd=required,
                      expires_at=now() + timedelta(seconds=ttl_seconds(multisig)))
        s.add(st)
        s.add(Action(strategy_id=st.id, action="create",
                     record={"intent_id": st.intent_id, "required_margin_usd": str(required),
                             "owner_pubkey": owner_pubkey, "owner_multisig": owner_multisig}))
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


# ---- authorization of mutating actions --------------------------------------

def authorize_action(sid: str, action: str, authorization, params: dict, org: str = "") -> tuple[Strategy, str]:
    """Load the strategy (404 first, so an unknown id never burns a nonce), then verify the signed message and that the
    signer owns it. Returns (strategy, who acted)."""
    st = get_strategy(sid, org)
    return st, auth.authorize(authorization, action, st, params)


def change_owner(sid: str, params: dict, authorization, org: str = "") -> Strategy:
    """A signed action by the CURRENT owner (or a member of the current owner multisig); attested."""
    new_pubkey, new_multisig = validate_owner(params.get("owner_pubkey"), params.get("owner_multisig"))
    st = get_strategy(sid, org)
    if st.status not in (S_PENDING, S_ACTIVE, S_REBALANCING):  # state first: a refused request must not burn a nonce
        raise ServiceError("CONFLICT", f"strategy is {st.status}; its owner can no longer be changed", 409)
    st, signer = authorize_action(sid, "change_owner", authorization, params, org)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        before = {"owner_pubkey": st.owner_pubkey, "owner_multisig": st.owner_multisig}
        st.owner_pubkey, st.owner_multisig, st.updated_at = new_pubkey, new_multisig, now()
        s.add(st)
        s.commit()
        fund = st.fund_id
    record = {"event": "change_owner", "from": before, "to": {"owner_pubkey": new_pubkey, "owner_multisig": new_multisig},
              "signed_by": signer, "authorization_nonce": (authorization or {}).get("nonce"),
              "authorization_timestamp": (authorization or {}).get("timestamp")}
    asig = _record_action(sid, "change_owner", record, fund, signed_by=signer, authorization=authorization)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.last_attestation_sig = asig
        s.add(st)
        s.commit()
    return get_strategy(sid, org)


def operator_set_owner(sid: str, owner_pubkey: str | None, owner_multisig: str | None) -> Strategy:
    """OPERATOR ONLY (CLI on the host; there is deliberately no API route for this). Binds an owner to a strategy that
    has none (e.g. one created before ownership existed). It cannot replace an existing owner: that is the owner's own
    signed `change_owner` action. Attested, with bound_by: operator."""
    new_pubkey, new_multisig = validate_owner(owner_pubkey, owner_multisig)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        if st is None:
            raise ServiceError("NOT_FOUND", "strategy not found", 404)
        if st.owner_pubkey or st.owner_multisig:
            raise ServiceError("CONFLICT", "this strategy already has an owner; only the owner can change it (signed change_owner)", 409)
        st.owner_pubkey, st.owner_multisig, st.updated_at = new_pubkey, new_multisig, now()
        s.add(st)
        s.commit()
        fund = st.fund_id
    record = {"event": "set_owner", "bound_by": "operator", "from": {"owner_pubkey": None, "owner_multisig": None},
              "to": {"owner_pubkey": new_pubkey, "owner_multisig": new_multisig}}
    asig = _record_action(sid, "set_owner", record, fund)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.last_attestation_sig = asig
        s.add(st)
        s.commit()
        s.refresh(st)
        return st


def require_liquidity(market_id: str, is_buy: bool, need: Decimal, what: str) -> None:
    """Before ANY order: the book must offer `need` on the side we would take, within the IOC's slippage band of the mark.
    Otherwise NO_LIQUIDITY and nothing is sent, instead of firing IOCs into an empty book. One rule for close, deploy and
    rebalance."""
    v = venue()
    mark = v.mark_price(market_id)
    limit = mark * (1 + DEFAULT_SLIPPAGE if is_buy else 1 - DEFAULT_SLIPPAGE)
    offered = v.available_liquidity(market_id, is_buy, limit)
    if offered < need:
        coin = state.markets[market_id].hl_coin
        raise ServiceError("NO_LIQUIDITY", f"the book offers {offered.normalize():f} {coin} to a {'buy' if is_buy else 'sell'} within "
                           f"{DEFAULT_SLIPPAGE:.1%} of the mark {mark} (limit {limit:.2f}), but {what} needs {need.normalize():f}. "
                           f"Nothing was sent. Start the market maker (sereel mm run --market {market_id}) and retry.", 409)


def operator_adjust_ledger(sid: str, realized_delta: Decimal, reason: str) -> tuple[Strategy, Decimal]:
    """OPERATOR ONLY (CLI on the host; no API route). Book a correction into a live strategy's realized P&L, with a written
    reason, attested. For known historical errors only (e.g. money that left a strategy's pot because of a since-fixed bug); it
    changes what the strategy is entitled to, so the reason is mandatory and the same reason cannot be booked twice."""
    reason = (reason or "").strip()
    if len(reason) < 20:
        raise _bad("a reason of at least 20 characters is required: say what happened, so the ledger entry explains itself")
    if realized_delta == 0:
        raise _bad("the adjustment is zero")
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        if st is None:
            raise ServiceError("NOT_FOUND", "strategy not found", 404)
        if st.status not in LIVE:
            raise ServiceError("CONFLICT", f"strategy is {st.status}; only a live strategy's ledger can be adjusted", 409)
        for a in s.exec(select(Action).where(Action.strategy_id == sid, Action.action == "ledger_adjustment")).all():
            if (a.record or {}).get("reason") == reason:
                raise ServiceError("CONFLICT", f"this reason was already booked on {a.created_at:%Y-%m-%d %H:%M} UTC; "
                                   "an adjustment is booked once", 409)
        before = st.realized_pnl_usd
        st.realized_pnl_usd = before + realized_delta
        st.updated_at = now()
        s.add(st)
        s.commit()
        after, fund = st.realized_pnl_usd, st.fund_id
    record = {"event": "ledger_adjustment", "booked_by": "operator", "field": "realized_pnl_usd", "delta": realized_delta,
              "before": before, "after": after, "reason": reason}
    asig = _record_action(sid, "ledger_adjustment", record, fund)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.last_attestation_sig = asig or st.last_attestation_sig
        s.add(st)
        s.commit()
        s.refresh(st)
    take_snapshot(sid, "ledger_adjustment")
    return st, before


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
    # a closing strategy whose margin was already released (margin 0) has nothing left on the venue
    return sum((r.margin_usd + r.realized_pnl_usd + r.funding_usd - r.fees_usd for r in rows
                if r.status != S_CLOSING or r.margin_usd > 0), Decimal(0))


RECONCILE_TOLERANCE = Decimal("0.05")  # rounding only: fees, realized P&L and unrealized P&L are all accounted for exactly


def ledger_equity(s: Session, marks: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """(cash, equity) the strategies are entitled to: credited margin + realized P&L + funding - fees, plus unrealized P&L at
    the venue's mark. A closing strategy whose margin was already released holds nothing on the venue any more."""
    cash = equity = Decimal(0)
    for r in s.exec(select(Strategy).where(Strategy.status.in_(LIVE))).all():
        if r.status == S_CLOSING and r.margin_usd <= 0:
            continue
        c = r.margin_usd + r.realized_pnl_usd + r.funding_usd - r.fees_usd
        cash += c
        equity += c + ((marks[r.market_id] - r.entry_px) * r.size if r.size else Decimal(0))
    return cash, equity


def reconcile() -> dict:
    """The venue's equity (accountValue) must equal what the strategies are entitled to, to within rounding.

    This compares EQUITY, not cash. Hyperliquid realizes a close against the blended account entry while the ledger realizes
    it against each strategy's own entry, so the two cash figures legitimately differ by an amount that reappears in
    unrealized P&L; in equity terms it cancels exactly, so a real shortfall cannot hide behind it (it once did)."""
    v = venue()
    first = next(iter(state.markets))
    pos = v.position(None, first)  # ONE snapshot: accountValue, unrealized P&L and entry come from the same instant
    with Session(engine) as s:
        used = set(s.exec(select(Strategy.market_id).where(Strategy.status.in_(LIVE))).all())
        # Mark the ledger at the price IMPLIED by that snapshot (entry + unrealized / size), not at a separate mark read: two
        # reads seconds apart differ by (position size x the price move between them), which is noise that looks like drift.
        implied = pos.entry_px + pos.unrealized_pnl / pos.size if pos.size else None
        marks = {m: (implied if (implied is not None and m == first) else v.mark_price(m)) for m in used}
        margins, (cash, equity) = sum_margins(s), ledger_equity(s, marks)
    venue_cash = pos.account_value - pos.unrealized_pnl
    gap = pos.account_value - equity  # negative: the venue holds LESS than the ledger says it should
    ok = gap >= -RECONCILE_TOLERANCE  # extra cash on the venue is unassigned (e.g. dust), not a shortfall
    if not ok:
        log.error("RECONCILIATION BREACH: venue equity %s is %s below the strategies' ledger equity %s", pos.account_value, -gap, equity)
    return {"ok": ok, "sum_strategy_margin_usd": num(margins), "ledger_cash_usd": num(cash), "dex_cash_usd": num(venue_cash),
            "ledger_equity_usd": num(equity), "venue_equity_usd": num(pos.account_value),
            "difference_usd": num(gap)}  # venue minus ledger equity


# ---- refunds ----------------------------------------------------------------

def _record_action(strategy_id: str, action: str, record: dict, fund_id: str, solana_signature: str | None = None,
                   hl_order_ids: list | None = None, signed_by: str | None = None, authorization: dict | None = None,
                   attest: bool = True, memo_extra: dict | None = None) -> str | None:
    """Store the full record and (unless attest=False) attest its hash on Solana. Returns the attestation signature."""
    sig = att.attest(strategy_id, fund_id, action, record, memo_extra) if attest else None
    with Session(engine) as s:
        s.add(Action(strategy_id=strategy_id, action=action, record=att.jsonable(record), solana_signature=solana_signature,
                     hl_order_ids=hl_order_ids or [], attestation_sig=sig, signer_public_key=signed_by,
                     authorization=authorization))
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

    Returns active | retry | failed | skipped | drift. Transient venue/price errors (including NO_LIQUIDITY) are retried on later
    watcher ticks for ACTIVATION_GRACE_S seconds measured from when funding completed (time, not attempts: attempts vary with how
    slow the venue is and how many ticks run); after that the strategy fails and the funding is refunded."""
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
                if st.funded_at is None:  # funded before this field existed: the window starts at the first attempt we see
                    st.funded_at = now()
                s.add(st)
                s.commit()
                if size > 0:  # before any money moves: an empty book must not even cost a margin transfer
                    require_liquidity(st.market_id, False, size, "opening the hedge")
                v.add_margin(st.market_id, received)  # exactly what was credited, never 'up to a target'
                fill = v.set_position(st.id, st.market_id, -size, current_size=Decimal(0)) if size > 0 else None
            except ServiceError as e:
                waited = (now() - st.funded_at).total_seconds()
                if waited >= settings.activation_grace_s or e.code == "UNKNOWN_MARKET":
                    outcome, reason = "failed", f"{e.code}: {e.message}"
                else:
                    left = settings.activation_grace_s - waited
                    st.failure_reason = (f"{e.code}: {e.message} (retrying, attempt {st.activation_attempts}; "
                                         f"gives up and refunds in {left:.0f}s)")
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
                st.funding_cursor_ms = int(time.time() * 1000)  # funding before the position existed is not ours
                s.add(st)
                s.commit()
                record = {"event": "deploy", "target_size": size, "filled": st.size, "remaining": fill.remaining if fill else 0,
                          "entry_px": st.entry_px, "fee": st.fees_usd, "margin_usd": received, "leverage": st.leverage,
                          "market": state.markets[st.market_id].hl_coin, "mark": v.mark_price(st.market_id),
                          "owner_pubkey": st.owner_pubkey, "owner_multisig": st.owner_multisig,  # bound at creation
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
        take_snapshot(sid, "deploy")
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


def activate_ready(skip: set[str] = frozenset()) -> int:
    """Retry activation for every fully funded strategy that is still pending (the first attempt may have hit a
    transient error). Returns how many were attempted."""
    with Session(engine) as s:
        ids = [st.id for st in s.exec(select(Strategy).where(Strategy.status == S_PENDING)).all()
               if (st.received_amount_usd or Decimal(0)) >= st.expected_amount_usd]
    ids = [i for i in ids if i not in skip]  # one that was just attempted this tick waits for the next one
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
                    v.add_margin(st.market_id, amount)
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
    decisions.resolve_executed(sid, "top_up")  # the top-up funding intent completed: a pending top-up proposal is answered
    take_snapshot(sid, "deposit")


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


# ---- the fill ledger, funding and P&L snapshots -----------------------------

def apply_fill(st: Strategy, delta: Decimal, px: Decimal, fee: Decimal) -> Decimal:
    """Book a fill of signed size `delta` at `px` into the strategy ledger; returns the realized P&L it produced.
    Growing a position blends the entry price; shrinking realizes P&L on the part closed (at the unchanged entry);
    flipping through zero realizes the closed part and opens the remainder at `px`."""
    old, realized = st.size, Decimal(0)
    if delta == 0:
        return realized
    if old == 0 or (old > 0) == (delta > 0):  # opening or growing
        st.entry_px = ((abs(old) * st.entry_px + abs(delta) * px) / (abs(old) + abs(delta))) if old else px
    else:
        closed = min(abs(delta), abs(old))
        realized = (px - st.entry_px) * closed * (1 if old > 0 else -1)  # long gains when px rises, short when it falls
        if abs(delta) > abs(old):  # flipped: the remainder opens at px
            st.entry_px = px
    st.size = old + delta
    if st.size == 0:
        st.entry_px = Decimal(0)
    st.realized_pnl_usd += realized
    st.fees_usd += fee
    return realized


def accrue_funding(sid: str) -> Decimal:
    """Book funding payments since this strategy's cursor. Funding is paid on the shared account position, so a strategy
    gets the share equal to its size over the account's size. Call BEFORE changing the size, so the share is right."""
    v = venue()
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        if st.status not in LIVE or st.size == 0:
            return Decimal(0)
        cursor = st.funding_cursor_ms or int(st.deployed_at.timestamp() * 1000)
        entries = v.funding_entries(st.market_id, cursor)
        if not entries:
            return Decimal(0)
        account = abs(v.position(None, st.market_id).size)
        share = min(abs(st.size) / account, Decimal(1)) if account else Decimal(0)
        paid = sum((usd for _, usd in entries), Decimal(0)) * share
        st.funding_usd += paid
        st.funding_cursor_ms = max(ms for ms, _ in entries)  # strictly-after on the next call: nothing counted twice
        s.add(st)
        s.commit()
        return paid


def values(st: Strategy, mark: Decimal | None) -> dict:
    """The v4 /value numbers. value_usd is margin + unrealized (the literal original definition); hedge_pnl is what
    the strategy earned and never includes margin, which is fund cash that merely moved."""
    unrealized = (mark - st.entry_px) * st.size if (mark is not None and st.size) else Decimal(0)
    hedge = unrealized + st.realized_pnl_usd + st.funding_usd - st.fees_usd
    return {"margin": st.margin_usd, "unrealized": unrealized, "realized": st.realized_pnl_usd, "funding": st.funding_usd,
            "fees": st.fees_usd, "value": st.margin_usd + unrealized, "hedge_pnl": hedge}


def take_snapshot(sid: str, cause: str) -> bool:
    """Store the P&L right now (every minute, and after every action). False if the venue price is unavailable."""
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        if st is None:
            return False
        mark = None
        if st.status in LIVE and st.size != 0:
            try:
                mark = venue().mark_price(st.market_id)
            except Exception as e:
                log.warning("snapshot of %s skipped, no mark: %s", sid, e)
                return False
        v = values(st, mark)
        s.add(PnlSnapshot(strategy_id=sid, unrealized_pnl_usd=v["unrealized"], realized_pnl_usd=v["realized"],
                          funding_usd=v["funding"], fees_usd=v["fees"], hedge_pnl_usd=v["hedge_pnl"], margin_usd=v["margin"],
                          cause=cause))
        s.commit()
        return True


def snapshot_all() -> int:
    """The once-a-minute job: accrue funding, then snapshot every strategy that holds a position."""
    with Session(engine) as s:
        ids = [x for x in s.exec(select(Strategy.id).where(Strategy.status.in_(LIVE))).all()]
    n = 0
    for sid in ids:
        try:
            accrue_funding(sid)
            n += take_snapshot(sid, "tick")
        except Exception:
            log.exception("snapshot of %s failed", sid)
    return n


def recover_interrupted() -> int:
    """A crash mid-rebalance leaves status 'rebalancing'. Put it back to active; ledger drift (a fill whose commit was
    lost) is then caught by the drift guard before anything else trades."""
    with Session(engine) as s:
        rows = s.exec(select(Strategy).where(Strategy.status == S_REBALANCING)).all()
        for st in rows:
            log.warning("strategy %s was interrupted mid-rebalance; marking it active (check /health reconciliation)", st.id)
            st.status = S_ACTIVE
            s.add(st)
        s.commit()
        return len(rows)


# ---- edit settings and rebalance ---------------------------------------------

def _parse_edit(params: dict) -> tuple[int | None, Decimal | None]:
    """Business validation of PATCH fields (they are already known to be well-formed strings)."""
    if not params:
        raise _bad("send hedge_ratio_bps and/or target_exposure_units")
    ratio = int(params["hedge_ratio_bps"]) if "hedge_ratio_bps" in params else None
    exposure = Decimal(params["target_exposure_units"]) if "target_exposure_units" in params else None
    if ratio is not None and not 0 <= ratio <= 10_000:
        raise _bad("hedge_ratio_bps must be between 0 and 10000")
    if exposure is not None and exposure <= 0:
        raise _bad("target_exposure_units must be positive")
    return ratio, exposure


def require_order_viable(st: Strategy, target_signed: Decimal, mark: Decimal, what: str, check_margin: bool = True) -> None:
    """Before a signature is spent: would moving this strategy to `target_signed` be an order the venue accepts? It refuses
    a trade worth less than the venue's minimum order (a full close to zero is exempt: it only reduces) and, when it grows
    the short, one the strategy's own cash cannot margin. Nothing is sent and no nonce is burned."""
    delta = target_signed - st.size
    unit = state.markets[st.market_id].unit if st.market_id in state.markets else "units"
    if delta == 0 or target_signed == 0:
        return
    notional, minimum = abs(delta) * mark, settings.min_order_usd * Decimal("1.05")  # cushion: the mark moves before the order
    if notional < minimum:
        need = (minimum / mark).quantize(Decimal("0.0001"), rounding="ROUND_UP")
        raise _bad(f"{what} would trade {abs(delta).normalize():f} {unit} (about ${notional:.2f}), below the venue's "
                   f"${settings.min_order_usd} minimum order. Nothing was sent. A change of at least {need.normalize():f} {unit} is needed; "
                   f"make a larger change or leave the target where it is")
    if check_margin and abs(target_signed) > abs(st.size):
        cash = st.margin_usd + st.realized_pnl_usd + st.funding_usd - st.fees_usd
        needed = abs(target_signed) * mark / st.leverage
        if needed > cash:
            raise ServiceError("INSUFFICIENT_MARGIN", f"a {abs(target_signed)} short at {st.leverage}x needs {needed:.2f} USD "
                               f"but the strategy holds {cash:.2f}; add margin with a top-up first. Nothing was sent")


def edit_settings(sid: str, params: dict, authorization, org: str = "") -> Strategy:
    """PATCH: change the hedge ratio and/or exposure. Moves the TARGET only; no order is sent (that is rebalance)."""
    ratio, exposure = _parse_edit(params)
    st = get_strategy(sid, org)
    if st.status != S_ACTIVE:
        raise ServiceError("CONFLICT", f"strategy is {st.status}; settings can only be edited while it is active", 409)
    mark = venue().mark_price(st.market_id)
    new_target = (exposure if exposure is not None else st.target_exposure_units) * \
        Decimal(ratio if ratio is not None else st.hedge_ratio_bps) / 10_000
    gap_bps = int(abs(new_target + st.size) / new_target * 10_000) if new_target else 0  # st.size is negative for a short
    if gap_bps > st.rebalance_band_bps:  # a rebalance would have to trade: make sure it could
        require_order_viable(st, -new_target, mark, "moving to this target", check_margin=False)  # margin: the owner tops up after the edit
    st, signer = authorize_action(sid, "edit_hedge_settings", authorization, params, org)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        before = {"hedge_ratio_bps": st.hedge_ratio_bps, "target_exposure_units": st.target_exposure_units,
                  "required_margin_usd": st.required_margin_usd}
        if ratio is not None:
            st.hedge_ratio_bps = ratio
        if exposure is not None:
            st.target_exposure_units = exposure
        st.required_margin_usd = required_margin(target_size(st), mark, st.leverage)
        st.updated_at = now()
        s.add(st)
        s.commit()
        after = {"hedge_ratio_bps": st.hedge_ratio_bps, "target_exposure_units": st.target_exposure_units,
                 "required_margin_usd": st.required_margin_usd}
        fund, size, target = st.fund_id, st.size, target_size(st)
    record = {"event": "edit_hedge_settings", "from": before, "to": after, "target_size": target, "current_size": size,
              "signed_by": signer, "authorization_nonce": (authorization or {}).get("nonce")}
    asig = _record_action(sid, "edit_hedge_settings", record, fund, signed_by=signer, authorization=authorization)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.last_attestation_sig = asig
        s.add(st)
        s.commit()
    take_snapshot(sid, "edit_hedge_settings")
    return get_strategy(sid, org)


def hedge_gap(st: Strategy) -> tuple[Decimal, int]:
    """(target short - current short in units, gap as basis points of the target). Positive = under-hedged."""
    target = target_size(st)
    gap = target - (-st.size)
    if target == 0:
        return gap, 10_000 if st.size != 0 else 0
    return gap, int(abs(gap) / target * 10_000)


def rebalance(sid: str, authorization, force: bool = False, org: str = "", agent_meta: dict | None = None) -> Strategy:
    """Move the hedge to its target if the gap is beyond the strategy's rebalance band (or `force`). Shrinking is
    reduce-only; growing needs the strategy's own capital to cover the new size at its leverage."""
    st = get_strategy(sid, org)
    if st.status != S_ACTIVE:
        raise ServiceError("CONFLICT", f"strategy is {st.status}; only an active strategy can be rebalanced", 409)
    v0 = venue()
    quantum0 = Decimal(1).scaleb(-v0.size_decimals(st.market_id))
    gap0, gap_bps0 = hedge_gap(st)
    if abs(gap0) >= quantum0 and (force or gap_bps0 > st.rebalance_band_bps):  # a trade would happen: is there a book to take it?
        delta0 = -target_size(st) - st.size  # signed size the rebalance would trade: > 0 buys (shrinking a short), < 0 sells
        require_order_viable(st, -target_size(st), v0.mark_price(st.market_id), "this rebalance")
        require_liquidity(st.market_id, delta0 > 0, abs(delta0), "this rebalance")
    st, signer = authorize_action(sid, "rebalance", authorization, {}, org)
    grant = delegates.grant_for_signer(sid, signer)  # None for the owner; a delegate acts within its signed limits
    v = venue()
    nonce = (authorization or {}).get("nonce")
    with account_lock(v.account_key):
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            if st.status != S_ACTIVE:  # something else changed it while we waited for the lock
                raise ServiceError("CONFLICT", f"strategy is {st.status}", 409)
            gap, gap_bps = hedge_gap(st)
            quantum = Decimal(1).scaleb(-v.size_decimals(st.market_id))
            if abs(gap) < quantum or (not force and gap_bps <= st.rebalance_band_bps):
                reason = "already at target" if abs(gap) < quantum else f"gap {gap_bps} bps is within the {st.rebalance_band_bps} bps band"
                fund = st.fund_id
                noop = True
            else:
                noop = False
                market_id, current_size = st.market_id, st.size  # read now: the session closes before the order is sent
                target_signed = -target_size(st)
                growing = abs(target_signed) > abs(st.size)
                mark = v.mark_price(st.market_id)
                if growing:
                    cash = st.margin_usd + st.realized_pnl_usd + st.funding_usd - st.fees_usd
                    needed = abs(target_signed) * mark / st.leverage
                    if needed > cash:
                        raise ServiceError("INSUFFICIENT_MARGIN", f"a {abs(target_signed)} short at {st.leverage}x needs {needed:.2f} USD "
                                           f"but the strategy holds {cash:.2f}; add margin with a top-up first")
                if ledger_drift(s, st.market_id):
                    raise ServiceError("CONFLICT", "the venue position differs from the ledger; rebalancing is held until it is reconciled", 409)
                require_liquidity(st.market_id, (target_signed - st.size) > 0, abs(target_signed - st.size), "this rebalance")  # again, now
                if grant is not None:  # a delegate's limits are enforced here, on every request, from what the owner signed
                    delegates.check_rebalance(grant, sid, force, abs(target_signed - st.size))
                st.status = S_REBALANCING
                s.add(st)
                s.commit()
        if noop:
            _record_action(sid, "rebalance", {"event": "rebalance", "traded": False, "reason": reason, "gap_units": gap,
                                              "gap_bps": gap_bps, "signed_by": signer, "authorization_nonce": nonce},
                           fund, signed_by=signer, authorization=authorization, attest=False)  # nothing happened on the venue
            return get_strategy(sid, org)
        accrue_funding(sid)  # before the size changes, so the funding share is right
        with Session(engine) as s:
            current_size = s.get(Strategy, sid).size  # accrual does not change it, but read it fresh under the lock anyway
        try:
            fill = v.set_position(sid, market_id, target_signed, current_size=current_size)
        except ServiceError:
            _restore_active(sid)
            raise
        with Session(engine) as s:
            st = s.get(Strategy, sid)
            before = {"size": st.size, "entry_px": st.entry_px}
            realized = apply_fill(st, fill.filled, fill.avg_px or mark, fill.fee)
            st.hl_order_ids = list(st.hl_order_ids or []) + list(fill.oids)
            st.status, st.updated_at = S_ACTIVE, now()
            s.add(st)
            s.commit()
            fund = st.fund_id
            record = {"event": "rebalance", "traded": True, "forced": force, "from": before, "target_size": target_signed,
                      "filled": fill.filled, "remaining": fill.remaining, "avg_px": fill.avg_px, "fee": fill.fee,
                      "realized_pnl_usd": realized, "size_after": st.size, "entry_px_after": st.entry_px, "hl_oids": fill.oids,
                      "reduce_only": [p.reduce_only for p in fill.partials], "signed_by": signer, "authorization_nonce": nonce,
                      "market_closed": v.market_closed.get(market_id, False)}
            if grant is not None:
                record["signed_by_role"] = "delegate"
                record["delegate_grant"] = {"max_rebalance_oz_per_day": grant.max_rebalance_oz_per_day, "expires_at": delegates.iso(grant.expires_at),
                                            "rebalance_within_band_only": grant.rebalance_within_band_only}
            if agent_meta:
                record["agent"] = agent_meta  # the state hash, every probability, the model ids and the decision id: the memo's hash commits to them
            oids = list(fill.oids)
    memo_extra = None
    if grant is not None:
        memo_extra = {"by": "delegate"}
        if agent_meta:  # compact: the state hash, the probabilities that triggered the action, the question set and the Jev model
            memo_extra.update({"sh": agent_meta["state_hash"], "p": agent_meta["trigger"], "q": agent_meta["question_set_version"], "m": agent_meta["model"]})
    asig = _record_action(sid, "rebalance", record, fund, hl_order_ids=oids, signed_by=signer, authorization=authorization, memo_extra=memo_extra)
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        st.last_attestation_sig = asig
        s.add(st)
        s.commit()
    decisions.resolve_executed(sid, "rebalance")  # a pending proposal of this type is now answered
    take_snapshot(sid, "rebalance")
    return get_strategy(sid, org)


def _restore_active(sid: str) -> None:
    with Session(engine) as s:
        st = s.get(Strategy, sid)
        if st and st.status == S_REBALANCING:
            st.status = S_ACTIVE
            s.add(st)
            s.commit()


# ---- /value and /history ------------------------------------------------------

def parse_as_of(text: str) -> datetime:
    """ISO 8601 (a naive time is UTC) or Unix milliseconds."""
    text = text.strip()
    try:
        if text.isdigit():
            return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        raise _bad(f"as_of '{text}' is not an ISO 8601 time or Unix milliseconds")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _last_attestation_before(s: Session, sid: str, at: datetime | None) -> str | None:
    q = select(Action).where(Action.strategy_id == sid, Action.attestation_sig.is_not(None)).order_by(Action.created_at.desc())
    if at is not None:
        q = q.where(Action.created_at <= at)
    a = s.exec(q).first()
    return a.attestation_sig if a else None


def _value_out(v: dict, as_of: datetime, attestation_sig: str | None, market_closed: bool | None) -> dict:
    return {"margin_usd": num(v["margin"]), "unrealized_pnl_usd": num(v["unrealized"]), "realized_pnl_usd": num(v["realized"]),
            "funding_usd": num(v["funding"]), "fees_usd": num(v["fees"]), "value_usd": num(v["value"]),
            "hedge_pnl_usd": num(v["hedge_pnl"]),  # unrealized + realized + funding - fees; margin is never in it
            "as_of": iso(as_of), "attestation_sig": attestation_sig,
            "attestation_url": sol.explorer_url(attestation_sig) if attestation_sig else None, "market_closed": market_closed}


def strategy_value(sid: str, org: str = "", as_of: str | None = None) -> dict:
    st = get_strategy(sid, org)
    if as_of is None:  # live
        mark = None
        if st.status in LIVE and st.size != 0:
            mark = mark_for(st.market_id)
            if mark is None:
                raise ServiceError("VENUE_UNAVAILABLE", "the venue returned no price, so a live value cannot be computed", 503)
        with Session(engine) as s:
            asig = st.last_attestation_sig or _last_attestation_before(s, sid, None)
        return _value_out(values(st, mark), now(), asig, bool(venue().market_closed.get(st.market_id, False)))
    at = parse_as_of(as_of)
    with Session(engine) as s:
        snap = s.exec(select(PnlSnapshot).where(PnlSnapshot.strategy_id == sid, PnlSnapshot.ts <= at)
                      .order_by(PnlSnapshot.ts.desc(), PnlSnapshot.id.desc())).first()
        if snap is None:
            raise ServiceError("NOT_FOUND", f"no snapshot of this strategy at or before {iso(at)}", 404)
        asig = _last_attestation_before(s, sid, snap.ts)
        v = {"margin": snap.margin_usd, "unrealized": snap.unrealized_pnl_usd, "realized": snap.realized_pnl_usd,
             "funding": snap.funding_usd, "fees": snap.fees_usd, "value": snap.margin_usd + snap.unrealized_pnl_usd,
             "hedge_pnl": snap.hedge_pnl_usd}
        ts = snap.ts
    return _value_out(v, ts, asig, None)


def strategy_history(sid: str, org: str = "") -> list[dict]:
    """Every action, oldest first: who signed it, the fill, fee, venue order ids, and the Solana signatures."""
    get_strategy(sid, org)
    with Session(engine) as s:
        actions = s.exec(select(Action).where(Action.strategy_id == sid).order_by(Action.created_at, Action.id)).all()
    out = []
    for a in actions:
        r = a.record or {}
        dec = lambda k: num(Decimal(r[k])) if r.get(k) not in (None, "") else None
        fill = {"size": dec("filled"), "avg_price_usd": dec("avg_px") if r.get("avg_px") else dec("entry_px"),
                "remaining": dec("remaining")} if r.get("filled") not in (None, "") else None
        out.append({"id": a.id, "type": a.action, "created_at": iso(a.created_at), "signed_by": a.signer_public_key,
                    "fill": fill, "fee_usd": dec("fee"), "realized_pnl_usd": dec("realized_pnl_usd"),
                    "hl_order_ids": list(a.hl_order_ids or []),
                    "solana_signature": a.solana_signature or r.get("funding_signature"),
                    "attestation_signature": a.attestation_sig,
                    "attestation_url": sol.explorer_url(a.attestation_sig) if a.attestation_sig else None, "details": r})
    return out


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
        liq = short_liquidation_price(cash, st.entry_px, magnitude, rate) if size < 0 \
            else (st.entry_px * magnitude - cash) / (magnitude * (1 - rate))
    venue_liq = venue_liquidation_for(st.market_id) if magnitude else None
    if liq is not None and venue_liq is not None:
        # the formula above assumes all of the strategy's margin backs the position; the venue's isolated margin can be
        # smaller. Report the nearer of the two, so the figure is never rosier than the venue's own.
        liq = min(liq, venue_liq) if size < 0 else max(liq, venue_liq)
    return {"side": "short" if size < 0 else "long" if size > 0 else "flat", "size_units": num(magnitude),
            "entry_price_usd": num(st.entry_px), "mark_price_usd": num(mark), "margin_usd": num(st.margin_usd),
            "unrealized_pnl_usd": num(unrealized), "realized_pnl_usd": num(st.realized_pnl_usd),
            # funding_paid_usd is positive when PAID; /value's funding_usd is the net amount received (the sign flips)
            "funding_paid_usd": num(-st.funding_usd), "fees_usd": num(st.fees_usd), "margin_health_bps": health,
            "maintenance_margin_usd": num(maintenance), "liquidation_price_usd": num(liq),
            "hl_order_ids": list(st.hl_order_ids or [])}


def data_feed_fields(strategy_id: str) -> dict:
    """data_feed_enabled (bool) and data_income_usd (decimal string): the opt-in switch and what the feed has earned the customer."""
    from ..x402 import feed

    cfg = feed.config(strategy_id)
    total, _, _ = feed.income(strategy_id)
    return {"data_feed_enabled": bool(cfg and cfg.enabled), "data_income_usd": feed.money_str(total)}


def strategy_out(st: Strategy) -> dict:
    mark = mark_for(st.market_id)
    size = target_size(st)
    received = st.received_amount_usd
    shortfall = max(st.expected_amount_usd - received, Decimal(0)) if received is not None and st.status == S_PENDING else None
    gap_units, gap_bps = hedge_gap(st) if st.status in LIVE else (None, None)
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
        "owner_user_id": st.owner_user_id, "owner_pubkey": st.owner_pubkey, "owner_multisig": st.owner_multisig,
        "created_at": iso(st.created_at), "updated_at": iso(st.updated_at),
        "deployed_at": iso(st.deployed_at), "closed_at": iso(st.closed_at), "position": position_out(st, mark),
        "market_closed": bool(venue().market_closed.get(st.market_id, False)), "failure_reason": st.failure_reason,
        "hedge_gap_units": num(gap_units), "hedge_gap_bps": gap_bps,  # target short minus current short; > 0 = under-hedged
        "agent_mode": decisions.agent_mode(st.id), "has_unread_proposal": decisions.has_unread_proposal(st.id),
        **data_feed_fields(st.id),
    }


def deposit_out(d: StrategyDeposit) -> dict:
    received = d.received_amount_usd
    shortfall = max(d.expected_amount_usd - received, Decimal(0)) if received is not None and d.status == D_PENDING else None
    return {"id": d.id, "strategy_id": d.strategy_id, "intent_id": d.intent_id, "amount_usd": num(d.amount_usd),
            "expected_amount_usd": num(d.expected_amount_usd), "received_amount_usd": num(received),
            "shortfall_usd": num(shortfall), "source_wallet_address": d.source_wallet_address,
            "registered_sender_address": d.registered_sender_address, "status": d.status, "expires_at": iso(d.expires_at),
            "solana_signature": d.solana_signature, "created_at": iso(d.created_at)}
