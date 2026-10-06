"""The deploy retry window is TIME since funding completed (ACTIVATION_GRACE_S), not a count of attempts.

Attempts varied from about a minute to over ten, because each attempt makes slow Hyperliquid calls and (once, by accident) two
watchers ran at once. A manager who has just funded should not be refunded because the market maker was a few seconds late,
and should not wait forever either.
"""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from sqlalchemy import inspect as sa_inspect
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.errors import ServiceError
from app.models import Strategy, now
from app.state import state
from app.strategies import service, watcher
from test_strategies import M, SENDER, age_funding, create, fund, get

D = Decimal


def row(sid):
    with Session(engine) as db:
        return db.get(Strategy, sid)


def stuck(api, fakechain):
    """A funded strategy whose deploy cannot open: nothing is offering to take the short."""
    state.venue.liquidity = D(0)
    s = create(api)
    fund(api, fakechain, s)
    return s


def test_the_default_window_is_two_minutes_and_the_old_attempts_knob_is_gone():
    assert settings.activation_grace_s == 120
    assert not hasattr(settings, "max_activation_attempts")  # one knob, not two competing ones


def test_funded_at_is_stamped_when_funding_completes_not_when_the_strategy_is_created(api, fakechain):
    s = create(api)
    assert row(s["id"]).funded_at is None  # created, not funded
    fund(api, fakechain, s, amount=100)  # underfunded
    assert row(s["id"]).funded_at is None
    before = now()
    fund(api, fakechain, s, amount=27.2)  # now fully funded
    stamped = row(s["id"]).funded_at
    assert stamped is not None and before <= stamped <= now()


def test_funded_at_is_set_once_and_later_transfers_never_move_it(api, fakechain):
    s = stuck(api, fakechain)
    first = row(s["id"]).funded_at
    age_funding(s["id"], 50)
    aged = row(s["id"]).funded_at
    fund(api, fakechain, s, amount=10)  # an extra transfer with the same memo, after funding completed
    assert row(s["id"]).funded_at == aged != first


def test_inside_the_window_it_waits_however_many_ticks_run(api, fakechain):
    s = stuck(api, fakechain)
    for _ in range(40):  # far more ticks than the old 12 attempts
        watcher.watch_once()
    a = get(api, s["id"])
    assert a["status"] == "pending_funding" and fakechain.refunds == [] and state.venue.orders == []
    assert row(s["id"]).activation_attempts > 12  # the count no longer decides anything
    assert "NO_LIQUIDITY" in a["failure_reason"] and "gives up and refunds in" in a["failure_reason"]


def test_rapid_repeated_attempts_from_several_watchers_cannot_shorten_the_window(api, fakechain):
    """Two watchers once ran at once and burned the old attempt budget in a fraction of the time."""
    s = stuck(api, fakechain)
    for _ in range(100):
        service.activate(s["id"])
    assert get(api, s["id"])["status"] == "pending_funding" and fakechain.refunds == []


def test_the_message_counts_down_the_time_left(api, fakechain):
    s = stuck(api, fakechain)
    age_funding(s["id"], 90)
    watcher.watch_once()
    left = int(get(api, s["id"])["failure_reason"].split("refunds in ")[1].split("s")[0])
    assert 20 <= left <= 30  # 120 - 90, give or take a few seconds of test time


@pytest.mark.parametrize("age,fails", [(0, False), (60, False), (115, False), (119, False), (120, True), (121, True), (600, True)])
def test_the_boundary_is_exactly_the_grace_period(api, fakechain, age, fails):
    s = stuck(api, fakechain)
    age_funding(s["id"], age)
    watcher.watch_once()
    a = get(api, s["id"])
    assert (a["status"] == "failed") is fails, a["failure_reason"]
    assert bool(fakechain.refunds) is fails


def test_when_the_window_runs_out_it_fails_clearly_refunds_and_attests(api, fakechain):
    s = stuck(api, fakechain)
    age_funding(s["id"], 120)
    watcher.watch_once()
    a = get(api, s["id"])
    assert a["status"] == "failed" and a["failure_reason"].startswith("NO_LIQUIDITY") and "Nothing was sent" in a["failure_reason"]
    assert fakechain.refunds[0][:2] == (SENDER, D("127.2")) and state.venue.orders == []
    assert [json.loads(m)["a"] for m in fakechain.memos] == ["deploy_failed", "refund"]
    assert watcher.watch_once()["activations"] == 0  # a failed strategy is not retried again


def test_the_book_returning_inside_the_window_activates_it_without_a_refund(api, fakechain):
    s = stuck(api, fakechain)
    age_funding(s["id"], 100)  # 20 seconds left
    watcher.watch_once()
    assert get(api, s["id"])["status"] == "pending_funding"
    state.venue.liquidity = None  # the market maker started
    assert watcher.watch_once()["activations"] == 1
    a = get(api, s["id"])
    assert a["status"] == "active" and a["failure_reason"] is None and fakechain.refunds == []
    assert state.venue.position(None, M).size == D("-0.12")


def test_every_kind_of_transient_failure_shares_the_one_window(api, fakechain):
    s = create(api)
    state.venue.price_override[M] = D("3000")  # a price deviation, not a missing book
    fund(api, fakechain, s)
    for _ in range(20):
        watcher.watch_once()
    assert get(api, s["id"])["status"] == "pending_funding"
    age_funding(s["id"], 120)
    watcher.watch_once()
    a = get(api, s["id"])
    assert a["status"] == "failed" and "PRICE_DEVIATION" in a["failure_reason"] and fakechain.refunds


def test_the_window_is_configurable_and_zero_means_fail_on_the_first_refusal(api, fakechain, monkeypatch):
    monkeypatch.setattr(settings, "activation_grace_s", 0)
    stuck(api, fakechain)  # one attempt, refused: no waiting
    assert fakechain.refunds
    fakechain.refunds.clear()
    monkeypatch.setattr(settings, "activation_grace_s", 600)
    s2 = create(api, registered_sender_address=str(Keypair().pubkey()))
    fund(api, fakechain, s2, sender=s2["registered_sender_address"])
    age_funding(s2["id"], 300)
    watcher.watch_once()
    assert get(api, s2["id"])["status"] == "pending_funding"  # 300 s into a 600 s window


def test_a_strategy_funded_before_funded_at_existed_gets_its_window_from_its_first_attempt(api, fakechain):
    """Rows funded before migration 0004 have funded_at NULL: they must not fail instantly nor wait forever."""
    state.venue.liquidity = D(0)
    s = create(api)
    with Session(engine) as db:  # funded, but with no funded_at (as an old row would be)
        st = db.get(Strategy, s["id"])
        st.received_amount_usd = st.expected_amount_usd
        st.funded_at = None
        db.add(st)
        db.commit()
    assert service.activate(s["id"]) == "retry"  # first attempt stamps the window
    stamped = row(s["id"]).funded_at
    assert stamped is not None and (now() - stamped).total_seconds() < 5 and get(api, s["id"])["status"] == "pending_funding"
    age_funding(s["id"], 121)
    assert service.activate(s["id"]) == "failed"


def test_an_unknown_market_is_not_retried_at_all(api, fakechain, monkeypatch):
    s = create(api)
    state.venue.liquidity = D(0)

    def boom(*a, **k):
        raise ServiceError("UNKNOWN_MARKET", "gone", 404)

    monkeypatch.setattr(service, "require_liquidity", boom)
    fund(api, fakechain, s)
    assert get(api, s["id"])["status"] == "failed" and fakechain.refunds  # nothing to wait for: fail inside the window


def test_the_migration_adds_a_nullable_column_so_existing_rows_survive():
    cols = {c["name"]: c for c in sa_inspect(engine).get_columns("strategy")}
    assert "funded_at" in cols and cols["funded_at"]["nullable"] is True
