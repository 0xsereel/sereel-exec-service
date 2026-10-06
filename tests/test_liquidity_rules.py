"""NO_LIQUIDITY applies to deploy, rebalance and close alike: checked before anything is sent."""
import json
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import UsedNonce
from app.state import state
from app.strategies import service, watcher
from test_step1 import M, OWNER, active, create, fund, get, patch, rebalance, set_price
from test_strategies import age_funding, rows

D = Decimal


def nonces():
    with Session(engine) as db:
        return len(db.exec(select(UsedNonce)).all())


# ---- the shared rule ------------------------------------------------------------------

def test_the_shared_check_names_the_numbers_and_the_remedy(api, fakechain):
    state.venue.liquidity = D("0.05")
    with pytest.raises(Exception) as e:
        service.require_liquidity(M, True, D("0.12"), "this rebalance")
    err = e.value
    assert (err.code, err.status) == ("NO_LIQUIDITY", 409)
    assert "offers 0.05" in err.message and "buy" in err.message and "0.5%" in err.message and "needs 0.12" in err.message
    assert "Nothing was sent" in err.message and f"sereel mm run --market {M}" in err.message
    service.require_liquidity(M, False, D("0.05"), "x")  # exactly enough is enough
    state.venue.liquidity = None
    service.require_liquidity(M, True, D("999"), "x")  # unlimited


# ---- deploy ----------------------------------------------------------------------------

def test_deploy_into_an_empty_book_sends_nothing_and_moves_no_margin(api, fakechain):
    state.venue.liquidity = D(0)
    funds_before = state.venue.funds
    s = create(api)
    fund(api, fakechain, s)
    a = get(api, s["id"])
    assert a["status"] == "pending_funding" and a["failure_reason"].startswith("NO_LIQUIDITY") and "attempt 1" in a["failure_reason"]
    assert "opening the hedge needs 0.12" in a["failure_reason"] and "Nothing was sent" in a["failure_reason"]
    assert state.venue.orders == [] and state.venue.funds == funds_before and fakechain.refunds == []  # no order, no margin transfer
    assert a["received_amount_usd"] == 127.2  # the money is safe and still the strategy's


def test_deploy_activates_on_a_later_tick_once_the_book_is_back(api, fakechain):
    state.venue.liquidity = D(0)
    s = create(api)
    fund(api, fakechain, s)
    watcher.watch_once()  # still empty
    assert get(api, s["id"])["status"] == "pending_funding" and state.venue.orders == []
    state.venue.liquidity = None  # the market maker started
    assert watcher.watch_once()["activations"] == 1
    assert get(api, s["id"])["status"] == "active" and state.venue.position(None, M).size == D("-0.12")


def test_deploy_that_never_finds_liquidity_fails_clearly_and_refunds(api, fakechain):
    state.venue.liquidity = D(0)
    s = create(api)
    fund(api, fakechain, s)
    for _ in range(3):
        watcher.watch_once()
    assert get(api, s["id"])["status"] == "pending_funding"  # still inside the window: waiting, not failing
    age_funding(s["id"], settings.activation_grace_s)
    watcher.watch_once()
    a = get(api, s["id"])
    assert a["status"] == "failed" and a["failure_reason"].startswith("NO_LIQUIDITY")
    assert state.venue.orders == [] and fakechain.refunds[0][:2] == (s["registered_sender_address"], D("127.2"))


def test_deploy_needs_the_whole_size_not_just_some_of_it(api, fakechain):
    state.venue.liquidity = D("0.05")  # a book that could fill part of the 0.12
    s = create(api)
    fund(api, fakechain, s)
    assert get(api, s["id"])["status"] == "pending_funding" and state.venue.orders == []
    state.venue.liquidity = D("0.12")
    watcher.watch_once()
    assert get(api, s["id"])["status"] == "active"


def test_a_zero_hedge_deploy_needs_no_book_at_all(api, fakechain):
    state.venue.liquidity = D(0)
    s = create(api, hedge_ratio_bps=0, expected_amount_usd=1)
    fund(api, fakechain, s, amount=1)
    assert get(api, s["id"])["status"] == "active"  # nothing to trade, so nothing to check


# ---- rebalance ----------------------------------------------------------------------------

def test_rebalance_into_an_empty_book_is_refused_before_the_signature_is_spent(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})  # target 0.06: a 0.06 buy-back
    orders, used = len(state.venue.orders), nonces()
    state.venue.liquidity = D("0.02")
    r, body = rebalance(api, s["id"])
    assert r.status_code == 409 and r.json()["code"] == "NO_LIQUIDITY" and set(r.json()) == {"error", "code"}
    assert "offers 0.02" in r.json()["error"] and "this rebalance needs 0.06" in r.json()["error"] and "buy" in r.json()["error"]
    a = get(api, s["id"])
    assert a["status"] == "active" and a["position"]["size_units"] == 0.12 and len(state.venue.orders) == orders
    assert nonces() == used  # not spent: the identical signed request works once the book is back
    state.venue.liquidity = None
    again = api.post(f"/strategies/{s['id']}/rebalance", json=body)
    assert again.status_code == 200 and again.json()["position"]["size_units"] == pytest.approx(0.06)


def test_a_rebalance_that_would_sell_checks_the_bid_side_size_it_needs(api, fakechain):
    s = active(api, fakechain, amount=300)
    patch(api, s["id"], {"target_exposure_units": "0.3"})  # target 0.18: sell 0.06 more
    state.venue.liquidity = D("0.05")
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 409 and "to a sell" in r.json()["error"] and "needs 0.06" in r.json()["error"]
    state.venue.liquidity = D("0.06")
    assert rebalance(api, s["id"])[0].status_code == 200


def test_a_within_band_or_no_op_rebalance_needs_no_book(api, fakechain):
    s = active(api, fakechain)
    state.venue.liquidity = D(0)
    r, _ = rebalance(api, s["id"])  # already at target
    assert r.status_code == 200 and state.venue.orders[-1:] == state.venue.orders[:1]
    patch(api, s["id"], {"target_exposure_units": "0.205"})  # 2.4% gap, inside the 5% band
    assert rebalance(api, s["id"])[0].status_code == 200
    forced, _ = rebalance(api, s["id"], force=True)  # force makes it a real trade: now the book matters
    assert forced.status_code == 409 and forced.json()["code"] == "NO_LIQUIDITY"


def test_liquidity_that_vanishes_between_the_check_and_the_trade_leaves_the_strategy_active(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    calls = {"n": 0}
    real = service.require_liquidity

    def second_call_fails(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:  # the check under the account lock, after the signature was verified
            state.venue.liquidity = D(0)
        return real(*a, **k)

    monkeypatch.setattr(service, "require_liquidity", second_call_fails)
    orders = len(state.venue.orders)
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 409 and r.json()["code"] == "NO_LIQUIDITY" and calls["n"] == 2
    a = get(api, s["id"])
    assert a["status"] == "active" and a["position"]["size_units"] == 0.12 and len(state.venue.orders) == orders


# ---- close uses the same rule (and message) ------------------------------------------------

def test_close_and_rebalance_say_the_same_thing_about_the_same_book(api, fakechain):
    from test_step2 import close

    s = active(api, fakechain)
    state.venue.liquidity = D("0.01")
    c, _ = close(api, s["id"])
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    r, _ = rebalance(api, s["id"])
    assert c.status_code == r.status_code == 409 and c.json()["code"] == r.json()["code"] == "NO_LIQUIDITY"
    head = lambda m: m.split(", but ")[0]  # "the book offers X to a buy within 0.5% of the mark M (limit L)": identical for the same book
    assert head(c.json()["error"]) == head(r.json()["error"])
    assert "closing needs 0.12" in c.json()["error"] and "this rebalance needs 0.06" in r.json()["error"]
    assert c.json()["error"].endswith("Nothing was sent. Start the market maker (sereel mm run --market XAU-HL) and retry.")
    assert r.json()["error"].endswith("Nothing was sent. Start the market maker (sereel mm run --market XAU-HL) and retry.")  # same remedy
