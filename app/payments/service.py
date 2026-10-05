"""Payouts: one-off sends and recurring schedules (stand-in for revenue and on/off ramps)."""
import logging
import random
import string
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from .. import pyth
from .. import solana_client as sol
from ..db import engine
from ..errors import ServiceError
from ..models import Payment, Schedule, now

log = logging.getLogger("sereel.payments")

MIN_INTERVAL_S = 5
MAX_MEMO_BYTES = 500
MEMO_FIELDS = {"seq", "units", "price", "amount"}
MAX_CONSECUTIVE_FAILURES = 3
STALE_CLAIM_S = 120
Q6 = Decimal("0.000001")


class PricingIn(BaseModel):
    pyth_feed_id: str
    units_per_payment: Decimal
    units_jitter_pct: Decimal = Decimal(0)


class ScheduleIn(BaseModel):
    name: str
    to: str
    interval_seconds: int
    amount_mode: Literal["fixed", "priced"] = "fixed"
    amount_usd: Decimal | None = None
    pricing: PricingIn | None = None
    memo_template: str = "Revenue payment {seq}"
    max_payments: int | None = None
    end_at: datetime | None = None
    fund_id: str | None = None
    start_immediately: bool = False


def _bad(msg: str) -> ServiceError:
    return ServiceError("BAD_REQUEST", msg, 400)


def check_address(addr: str) -> None:
    if not sol.is_valid_address(addr):
        raise _bad(f"'{addr}' is not a valid Solana address")


def check_template(template: str) -> None:
    """Only {seq} {units} {price} {amount}; no attribute or index access."""
    try:
        fields = list(string.Formatter().parse(template))
    except ValueError as e:
        raise _bad(f"bad memo template: {e}")
    for _, field, spec, conv in fields:
        if field is None:
            continue
        if field not in MEMO_FIELDS or conv:
            raise _bad(f"memo template may only use {sorted(MEMO_FIELDS)}, got {{{field}}}")
    if len(render_memo(template, 1, Decimal("1"), Decimal("1"), Decimal("1")).encode()) > MAX_MEMO_BYTES:
        raise _bad(f"memo longer than {MAX_MEMO_BYTES} bytes")


def render_memo(template: str, seq: int, units: Decimal | None, price: Decimal | None, amount: Decimal) -> str:
    fmt = lambda d: "-" if d is None else f"{d.normalize():f}"
    memo = template.format(seq=seq, units=fmt(units), price=fmt(price), amount=fmt(amount))
    if len(memo.encode()) > MAX_MEMO_BYTES:
        raise _bad(f"memo longer than {MAX_MEMO_BYTES} bytes")
    return memo


# ---- one-off ----------------------------------------------------------------

def send_payment(to: str, amount_usd: Decimal, memo: str = "", schedule_id: str | None = None,
                 seq: int | None = None, payment: Payment | None = None) -> Payment:
    """Record intent first (status claimed), then send, then record the result."""
    check_address(to)
    amount = Decimal(amount_usd).quantize(Q6)
    if amount <= 0:
        raise _bad("amount_usd must be positive")
    if len(memo.encode()) > MAX_MEMO_BYTES:
        raise _bad(f"memo longer than {MAX_MEMO_BYTES} bytes")
    with Session(engine) as s:
        if payment is None:
            payment = Payment(schedule_id=schedule_id, seq=seq, to=to, amount_usd=amount, memo=memo)
            s.add(payment)
            s.commit()
        pid = payment.id
    try:
        sig = sol.pay(to, amount, memo or None)
    except Exception as e:
        _finish(pid, "failed", error=str(e))
        raise ServiceError("PAYMENT_FAILED", f"payment failed: {e}", 502)
    return _finish(pid, "sent", signature=sig)


def _finish(pid: str, status: str, signature: str | None = None, error: str | None = None) -> Payment:
    with Session(engine) as s:
        p = s.get(Payment, pid)
        p.status, p.signature, p.error = status, signature, error
        s.add(p)
        if status == "sent" and p.schedule_id:
            sc = s.get(Schedule, p.schedule_id)
            sc.total_paid_usd += p.amount_usd
            s.add(sc)
        s.commit()
        s.refresh(p)
        return p


# ---- schedules --------------------------------------------------------------

def create_schedule(body: ScheduleIn) -> Schedule:
    check_address(body.to)
    check_template(body.memo_template)
    if not body.name.strip():
        raise _bad("name is required")
    if body.interval_seconds < MIN_INTERVAL_S:
        raise _bad(f"interval_seconds must be at least {MIN_INTERVAL_S}")
    if body.max_payments is not None and body.max_payments < 1:
        raise _bad("max_payments must be at least 1")
    if body.amount_mode == "fixed" and (body.amount_usd is None or body.amount_usd <= 0):
        raise _bad("amount_mode 'fixed' needs a positive amount_usd")
    if body.amount_mode == "priced":
        if body.pricing is None or body.pricing.units_per_payment <= 0 or not 0 <= body.pricing.units_jitter_pct < 100:
            raise _bad("amount_mode 'priced' needs pricing with units_per_payment > 0 and 0 <= units_jitter_pct < 100")
    first = now() if body.start_immediately else now() + timedelta(seconds=body.interval_seconds)
    with Session(engine) as s:
        if s.exec(select(Schedule).where(Schedule.name == body.name, Schedule.status.in_(("active", "paused")))).first():
            raise ServiceError("CONFLICT", f"a live schedule named '{body.name}' already exists", 409)
        sc = Schedule(name=body.name, fund_id=body.fund_id, to=body.to, interval_seconds=body.interval_seconds,
                      amount_mode=body.amount_mode, amount_usd=body.amount_usd or Decimal(0),
                      pricing=body.pricing.model_dump(mode="json") if body.pricing else None,
                      memo_template=body.memo_template, max_payments=body.max_payments, end_at=body.end_at, next_run=first)
        s.add(sc)
        s.commit()
        s.refresh(sc)
        return sc


def get_schedule(ref: str) -> Schedule:
    """Look up by id or, failing that, by name (live ones first)."""
    with Session(engine) as s:
        sc = s.get(Schedule, ref)
        if not sc:
            rows = s.exec(select(Schedule).where(Schedule.name == ref)).all()
            sc = next((r for r in rows if r.status in ("active", "paused")), rows[-1] if rows else None)
        if not sc:
            raise ServiceError("NOT_FOUND", f"no schedule '{ref}'", 404)
        return sc


def list_schedules() -> list[Schedule]:
    with Session(engine) as s:
        return list(s.exec(select(Schedule).order_by(Schedule.created_at)).all())


def set_status(ref: str, status: str) -> Schedule:
    """active <-> paused, or stopped (final)."""
    sc = get_schedule(ref)
    if sc.status in ("stopped", "done"):
        raise ServiceError("CONFLICT", f"schedule is {sc.status}", 409)
    if status not in ("active", "paused", "stopped"):
        raise _bad("status must be active, paused or stopped")
    with Session(engine) as s:
        sc = s.get(Schedule, sc.id)
        sc.status = status
        if status == "active":  # resuming: do not pay for the time spent paused
            sc.next_run = now() + timedelta(seconds=sc.interval_seconds)
        s.add(sc)
        s.commit()
        s.refresh(sc)
        return sc


def list_payments(to: str | None = None, limit: int = 200) -> list[Payment]:
    with Session(engine) as s:
        q = select(Payment).order_by(Payment.created_at.desc()).limit(limit)
        if to:
            q = q.where(Payment.to == to)
        return list(s.exec(q).all())


# ---- running ----------------------------------------------------------------

def compute_amount(sc: Schedule) -> tuple[Decimal, Decimal | None, Decimal | None]:
    """(amount_usd, units, price). Priced: units (with +/- jitter) times the live Pyth price."""
    if sc.amount_mode == "fixed":
        return sc.amount_usd.quantize(Q6), None, None
    pr = sc.pricing
    price = pyth.get_price(pr["pyth_feed_id"], 60).price
    jitter = Decimal(str(random.uniform(-1, 1))) * Decimal(str(pr["units_jitter_pct"])) / 100
    units = (Decimal(str(pr["units_per_payment"])) * (1 + jitter)).quantize(Decimal("0.0001"))
    return (units * price).quantize(Q6), units, price


def _claim(sc_id: str, expect_seq: int, interval: int, amount: Decimal, to: str, memo: str, units, price,
           at: datetime | None = None) -> Payment | None:
    """Atomically take run number expect_seq+1. Returns the claimed Payment, or None if somebody else got there."""
    seq = expect_seq + 1
    with Session(engine) as s:
        res = s.exec(update(Schedule).where(Schedule.id == sc_id, Schedule.seq == expect_seq, Schedule.status == "active")
                     .values(seq=seq, next_run=(at or now()) + timedelta(seconds=interval)))
        if res.rowcount != 1:
            s.rollback()
            return None
        p = Payment(schedule_id=sc_id, seq=seq, to=to, amount_usd=amount, memo=memo)
        s.add(p)
        try:
            s.commit()
        except Exception:  # unique (schedule_id, seq) violated: already claimed elsewhere
            s.rollback()
            return None
        s.refresh(p)
        return p


def run_due(at: datetime | None = None) -> list[Payment]:
    """One scheduler tick: pay every active schedule that is due. Safe to call from several processes."""
    at = at or now()
    with Session(engine) as s:
        due = list(s.exec(select(Schedule).where(Schedule.status == "active", Schedule.next_run <= at)).all())
    paid = []
    for sc in due:
        try:
            p = _run_one(sc, at)
            if p:
                paid.append(p)
        except Exception:
            log.exception("schedule %s (%s) failed", sc.name, sc.id)
    return paid


def _run_one(sc: Schedule, at: datetime) -> Payment | None:
    if sc.end_at and at >= sc.end_at.replace(tzinfo=at.tzinfo):
        _set_done(sc.id, "end_at reached")
        return None
    if sc.max_payments is not None and sc.seq >= sc.max_payments:
        _set_done(sc.id, "max_payments reached")
        return None
    try:
        amount, units, price = compute_amount(sc)  # before claiming: a price outage must not burn a run
    except ServiceError as e:
        log.warning("schedule %s skipped this tick: %s", sc.name, e.message)
        return None
    memo = render_memo(sc.memo_template, sc.seq + 1, units, price, amount)
    claimed = _claim(sc.id, sc.seq, sc.interval_seconds, amount, sc.to, memo, units, price, at)
    if not claimed:
        return None
    try:
        p = send_payment(sc.to, amount, memo, payment=claimed)
        log.info("schedule %s #%s paid %s -> %s", sc.name, claimed.seq, amount, p.signature)
    except ServiceError:
        p = None
        _after_failure(sc)
    if sc.max_payments is not None and claimed.seq >= sc.max_payments:
        _set_done(sc.id, "max_payments reached")
    return p


def _after_failure(sc: Schedule) -> None:
    with Session(engine) as s:
        last = s.exec(select(Payment).where(Payment.schedule_id == sc.id).order_by(Payment.seq.desc())
                      .limit(MAX_CONSECUTIVE_FAILURES)).all()
        if len(last) == MAX_CONSECUTIVE_FAILURES and all(p.status == "failed" for p in last):
            row = s.get(Schedule, sc.id)
            row.status = "paused"
            s.add(row)
            s.commit()
            log.error("schedule %s paused after %d consecutive failed payments", sc.name, MAX_CONSECUTIVE_FAILURES)


def _set_done(sc_id: str, why: str) -> None:
    with Session(engine) as s:
        sc = s.get(Schedule, sc_id)
        if sc.status == "active":
            sc.status = "done"
            s.add(sc)
            s.commit()
            log.info("schedule %s done: %s", sc.name, why)


def mark_unconfirmed_claims(max_age_s: int = STALE_CLAIM_S) -> int:
    """A claim that never reached sent/failed (process died mid-send) may or may not have gone out on-chain.
    Never resend it: mark it for manual review."""
    cutoff = now() - timedelta(seconds=max_age_s)
    with Session(engine) as s:
        rows = s.exec(select(Payment).where(Payment.status == "claimed", Payment.created_at < cutoff)).all()
        for p in rows:
            p.status, p.error = "unconfirmed", "process stopped before the send was confirmed; check the chain before retrying"
            s.add(p)
        s.commit()
        if rows:
            log.warning("%d payment(s) marked unconfirmed (not resent)", len(rows))
        return len(rows)
