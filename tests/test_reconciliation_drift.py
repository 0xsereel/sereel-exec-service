"""Regression for the live drift: a second strategy opened while the first had unrealized P&L, closed at another entry."""
from decimal import Decimal

import pytest
from solders.keypair import Keypair

from app.state import state
from app.strategies import service, withdrawals
from test_step1 import set_price
from test_step2 import DEST, M, OWNER, active, auth_close, close, create, fund, get

D = Decimal


def equity_gap(api):
    """venue equity minus ledger equity, as /health reports it."""
    r = api.get("/health").json()["reconciliation"]
    return r["difference_usd"], r


def test_a_second_strategy_funded_while_the_first_has_unrealized_pnl_does_not_leak(api, fakechain):
    s1 = active(api, fakechain, amount=200)
    set_price(2640)  # strategy 1's short is up (2650 -> 2640): the dex accountValue now includes that unrealized gain
    s2 = create(api, registered_sender_address=str(Keypair().pubkey()))
    fund(api, fakechain, s2, amount=150, sender=s2["registered_sender_address"])
    assert get(api, s2["id"])["status"] == "active"
    # the venue must have received what strategy 2 was credited, no matter what strategy 1's unrealized P&L was
    gap, r = equity_gap(api)
    assert abs(gap) < 1e-6, r
    set_price(2645)
    gap, r = equity_gap(api)
    assert abs(gap) < 1e-6, r  # and it stays exact as the price moves


def test_closing_the_second_strategy_at_a_different_entry_keeps_the_books_balanced(api, fakechain):
    """This is the live scenario: HL realizes the close against the blended account entry, the ledger against the strategy's
    own entry. In cash terms that differs; in equity terms it must cancel exactly."""
    s1 = active(api, fakechain, amount=200)
    set_price(2640)
    s2 = create(api, registered_sender_address=str(Keypair().pubkey()))
    fund(api, fakechain, s2, amount=150, sender=s2["registered_sender_address"])
    set_price(2650)
    params, a = auth_close(s2["id"])
    w = withdrawals.request_close(s2["id"], params, a)
    assert withdrawals.advance_withdrawal(w.id) == "completed"
    gap, r = equity_gap(api)
    assert abs(gap) < 1e-6 and r["ok"] is True, r
    # the cash view does NOT match (that is the redistribution), which is exactly why cash is the wrong thing to compare
    assert abs(r["dex_cash_usd"] - r["ledger_cash_usd"]) > 0.01
    # and the strategy that stayed is still exactly consistent with the venue
    s1_now = get(api, s1["id"])["position"]
    assert s1_now["size_units"] == 0.12 and state.venue.position(None, M).size == D("-0.12")


def test_the_gap_is_real_when_money_is_really_missing(api, fakechain):
    active(api, fakechain, amount=200)
    state.venue._cash[M] -= D("0.43")  # the live shortfall, re-created
    gap, r = equity_gap(api)
    assert gap == pytest.approx(-0.43, abs=1e-6) and r["ok"] is False  # 43 cents is NOT hidden by the tolerance


def test_unassigned_venue_cash_is_reported_but_not_an_alarm(api, fakechain):
    active(api, fakechain, amount=200)
    state.venue._cash[M] += D("0.0069")  # pre-existing dust on the dex that belongs to no strategy
    gap, r = equity_gap(api)
    assert gap == pytest.approx(0.0069, abs=1e-6) and r["ok"] is True


def test_the_tolerance_was_not_widened():
    assert service.RECONCILE_TOLERANCE == D("0.05")


def test_two_mark_reads_seconds_apart_do_not_look_like_drift(api, fakechain, monkeypatch):
    """Live: Hyperliquid was slow, the mark moved 0.30 between the venue's snapshot and the ledger's own mark read, and a
    0.12 oz position showed a phantom 0.036 'drift' with nothing traded. Both sides must use the same instant."""
    active(api, fakechain, amount=200)
    snap = state.venue.position(None, M)  # one consistent snapshot at the current mark
    monkeypatch.setattr(state.venue, "position", lambda *a: snap)
    set_price(2650.30)  # a later, separate mark read now returns a price 0.30 higher
    gap, r = equity_gap(api)
    assert abs(gap) < 1e-6, r  # the ledger was marked at the snapshot's implied price, not at the later one


def test_with_no_position_the_gap_still_works(api, fakechain):
    gap, r = equity_gap(api)  # nothing live: both sides are empty
    assert gap == 0 and r["ok"] is True and r["ledger_equity_usd"] == 0
