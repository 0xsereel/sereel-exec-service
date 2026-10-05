import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select

from app import solana_client as sol
from app.db import engine
from app.errors import ServiceError
from app.models import Payment, Schedule, now
from app.payments import service
from app.payments.service import ScheduleIn

D = Decimal
WALLET = str(Keypair().pubkey())
FEED = "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"


@pytest.fixture()
def chain(monkeypatch):
    """Fake sol.pay: records sends, can be made to fail."""
    class Chain:
        sent, fail = [], False

    c = Chain()
    c.sent = []

    def pay(to, amount, memo=None, source=None):
        if c.fail:
            raise sol.SolanaError("rpc down")
        c.sent.append((to, amount, memo))
        return f"sig{len(c.sent)}"

    monkeypatch.setattr(sol, "pay", pay)
    return c


def sched(**kw):
    base = dict(name="rev", to=WALLET, interval_seconds=60, amount_usd=D(1000), start_immediately=True)
    base.update(kw)
    return service.create_schedule(ScheduleIn(**base))


def rows(model):
    with Session(engine) as s:
        return list(s.exec(select(model)).all())


# ---- one-off ----------------------------------------------------------------

def test_send_records_claim_then_sent(chain):
    p = service.send_payment(WALLET, D("12.5"), "hello")
    assert (p.status, p.signature, p.amount_usd) == ("sent", "sig1", D("12.5")) and chain.sent == [(WALLET, D("12.5"), "hello")]


def test_send_failure_is_recorded_not_lost(chain):
    chain.fail = True
    with pytest.raises(ServiceError) as e:
        service.send_payment(WALLET, D(5))
    assert e.value.code == "PAYMENT_FAILED" and e.value.status == 502
    (p,) = rows(Payment)
    assert p.status == "failed" and "rpc down" in p.error


@pytest.mark.parametrize("to,amount,memo", [("nope", 1, ""), (WALLET, 0, ""), (WALLET, -3, ""), (WALLET, 1, "x" * 501)])
def test_send_validation(chain, to, amount, memo):
    with pytest.raises(ServiceError) as e:
        service.send_payment(to, D(amount), memo)
    assert e.value.code == "BAD_REQUEST" and chain.sent == [] and rows(Payment) == []


# ---- schedule validation ----------------------------------------------------

@pytest.mark.parametrize("kw", [
    dict(to="bad"), dict(interval_seconds=1), dict(amount_usd=None), dict(amount_usd=D(0)), dict(max_payments=0),
    dict(memo_template="{secret}"), dict(memo_template="{seq.__class__}"), dict(memo_template="{seq!r}"), dict(memo_template="{"),
    dict(amount_mode="priced"), dict(name=" "),
    dict(amount_mode="priced", pricing=dict(pyth_feed_id=FEED, units_per_payment=0)),
    dict(amount_mode="priced", pricing=dict(pyth_feed_id=FEED, units_per_payment=1, units_jitter_pct=100)),
])
def test_schedule_validation_rejects(kw):
    with pytest.raises(ServiceError) as e:
        sched(**kw)
    assert e.value.code == "BAD_REQUEST" and rows(Schedule) == []


def test_duplicate_live_name_conflicts_but_a_stopped_name_can_be_reused():
    sched()
    with pytest.raises(ServiceError) as e:
        sched()
    assert e.value.code == "CONFLICT" and e.value.status == 409
    service.set_status("rev", "stopped")
    assert sched().status == "active"


# ---- running ----------------------------------------------------------------

def test_due_schedule_pays_and_advances(chain):
    sc = sched(memo_template="Revenue payment {seq}")
    (p,) = service.run_due()
    assert chain.sent == [(WALLET, D(1000), "Revenue payment 1")] and p.seq == 1
    after = service.get_schedule(sc.id)
    assert after.seq == 1 and after.total_paid_usd == D(1000)
    assert timedelta(seconds=55) < after.next_run - now() <= timedelta(seconds=60)
    assert service.run_due() == [] and len(chain.sent) == 1  # not due again yet


def test_not_due_and_paused_do_not_pay(chain):
    sched(start_immediately=False)  # first run one interval from now
    assert service.run_due() == []
    service.set_status("rev", "paused")
    assert service.run_due(now() + timedelta(hours=1)) == [] and chain.sent == []


def test_resume_does_not_back_pay_for_the_paused_time(chain):
    sched()
    service.set_status("rev", "paused")
    service.set_status("rev", "active")
    assert service.run_due() == []  # next run pushed a full interval out


def test_downtime_pays_once_not_a_burst(chain):
    sched(interval_seconds=60)
    later = now() + timedelta(hours=5)  # service was down for 5 hours
    assert len(service.run_due(later)) == 1 and len(chain.sent) == 1
    assert service.run_due(later) == []  # next_run is a full interval after the catch-up, not 300 payments behind


def test_stops_after_max_payments_and_at_end_time(chain):
    sched(max_payments=2)
    for _ in range(4):
        service.run_due(now() + timedelta(days=1))
        with Session(engine) as s:  # make it due again
            sc = s.exec(select(Schedule)).one()
            sc.next_run = now() - timedelta(seconds=1)
            s.add(sc)
            s.commit()
    assert len(chain.sent) == 2 and service.get_schedule("rev").status == "done"
    service.create_schedule(ScheduleIn(name="timed", to=WALLET, interval_seconds=60, amount_usd=D(1), start_immediately=True,
                                       end_at=now() - timedelta(seconds=1)))
    service.run_due()
    assert len(chain.sent) == 2 and service.get_schedule("timed").status == "done"


def test_priced_amount_is_units_times_live_price_with_jitter(chain, monkeypatch):
    monkeypatch.setattr(service.random, "uniform", lambda a, b: 1)  # +jitter_pct exactly
    sched(amount_mode="priced", amount_usd=None, pricing=dict(pyth_feed_id=FEED, units_per_payment=20, units_jitter_pct=5),
          memo_template="Revenue payment {seq} | {units} units @ {price}")
    (p,) = service.run_due()
    # conftest mocks Pyth at 2650: units = 20 * 1.05 = 21, amount = 21 * 2650
    assert p.amount_usd == D(55650) and chain.sent[0][2] == "Revenue payment 1 | 21 units @ 2650"


def test_price_outage_skips_the_tick_without_burning_a_run(chain, monkeypatch):
    sched(amount_mode="priced", amount_usd=None, pricing=dict(pyth_feed_id=FEED, units_per_payment=1))

    def down(*a, **k):
        raise ServiceError("STALE_PRICE", "old", 503)

    monkeypatch.setattr(service.pyth, "get_price", down)
    assert service.run_due() == [] and chain.sent == [] and rows(Payment) == []
    assert service.get_schedule("rev").seq == 0  # nothing claimed, so the next tick retries the same run


def test_three_consecutive_failures_pause_the_schedule(chain):
    sched(interval_seconds=60)
    chain.fail = True
    for _ in range(3):
        service.run_due(now() + timedelta(days=1))
        with Session(engine) as s:
            sc = s.exec(select(Schedule)).one()
            sc.next_run = now() - timedelta(seconds=1)
            s.add(sc)
            s.commit()
    assert [p.status for p in rows(Payment)] == ["failed"] * 3 and service.get_schedule("rev").status == "paused"
    assert service.get_schedule("rev").total_paid_usd == 0


# ---- double-pay protection --------------------------------------------------

def test_a_run_can_only_be_claimed_once(chain):
    sc = sched()
    first = service._claim(sc.id, 0, 60, D(1), WALLET, "m", None, None)
    second = service._claim(sc.id, 0, 60, D(1), WALLET, "m", None, None)  # a second process with the same stale view
    assert first and second is None and len(rows(Payment)) == 1


def test_concurrent_ticks_pay_exactly_once(chain):
    sched()
    threads = [threading.Thread(target=service.run_due) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(chain.sent) == 1 and len(rows(Payment)) == 1


def test_restart_never_resends_a_claim_that_may_have_gone_out(chain):
    sc = sched()
    stale = service._claim(sc.id, 0, 60, D(1000), WALLET, "m", None, None)  # claimed, then the process died mid-send
    with Session(engine) as s:
        p = s.get(Payment, stale.id)
        p.created_at = now() - timedelta(minutes=10)
        s.add(p)
        s.commit()
    assert service.mark_unconfirmed_claims() == 1
    assert rows(Payment)[0].status == "unconfirmed" and chain.sent == []
    assert service.run_due(now() + timedelta(seconds=1)) == [] and chain.sent == []  # still not paid again
    assert service.get_schedule(sc.id).seq == 1


def test_fresh_claims_are_not_touched_by_the_unconfirmed_sweep(chain):
    sc = sched()
    service._claim(sc.id, 0, 60, D(1), WALLET, "m", None, None)
    assert service.mark_unconfirmed_claims() == 0 and rows(Payment)[0].status == "claimed"
