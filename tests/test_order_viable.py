"""Rebalance and edit refuse, before a signature is spent, a move the venue could never accept."""
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.db import engine
from app.models import Strategy, UsedNonce
from app.state import state
from app.strategies import service
from test_step1 import M, active, get, patch, rebalance, set_price

D = Decimal


def nonces():
    with Session(engine) as db:
        return len(db.exec(select(UsedNonce)).all())


def test_a_forced_rebalance_under_the_venue_minimum_is_refused_and_the_signature_is_kept(api, fakechain):
    s = active(api, fakechain)  # price 2650: hedge 0.12
    patch(api, s["id"], {"target_exposure_units": "0.205"})  # target 0.123: a 0.003 trade, about $8
    orders, used = len(state.venue.orders), nonces()
    r, body = rebalance(api, s["id"], force=True)
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and set(r.json()) == {"error", "code"}
    e = r.json()["error"]
    assert "this rebalance would trade 0.003 oz (about $7.95)" in e and "below the venue's $10 minimum order" in e
    assert "Nothing was sent" in e and "at least 0.004 oz is needed" in e and "units" not in e
    assert len(state.venue.orders) == orders and nonces() == used  # nothing sent, nonce not burned
    assert get(api, s["id"])["position"]["size_units"] == 0.12


def test_a_rebalance_the_strategy_cannot_margin_is_refused_before_the_nonce_is_spent(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.5"})  # target 0.3: needs 265 USD at 3x, the strategy holds 127.2
    orders, used = len(state.venue.orders), nonces()
    r, body = rebalance(api, s["id"])
    assert r.status_code == 400 and r.json()["code"] == "INSUFFICIENT_MARGIN" and set(r.json()) == {"error", "code"}
    assert "needs 265.00 USD" in r.json()["error"] and "top-up" in r.json()["error"] and "Nothing was sent" in r.json()["error"]
    assert len(state.venue.orders) == orders and nonces() == used  # the identical signed request is still usable


def small_active(api, fakechain):
    from test_strategies import create, fund
    s = create(api, target_exposure_units=0.05, hedge_ratio_bps=6000, expected_amount_usd=40)  # hedge 0.03 oz = $79
    fund(api, fakechain, s, amount=40)
    assert get(api, s["id"])["status"] == "active"
    return s


def test_an_edit_whose_rebalance_could_never_trade_is_refused_and_stores_nothing(api, fakechain):
    s = small_active(api, fakechain)
    used = nonces()
    r, _ = patch(api, s["id"], {"hedge_ratio_bps": "6700"})  # target 0.0335: 11.7% out of band, a 0.0035 trade = $9.28
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and set(r.json()) == {"error", "code"}
    assert "moving to this target would trade 0.0035 oz (about $9.28)" in r.json()["error"]
    assert "Nothing was sent" in r.json()["error"]
    assert get(api, s["id"])["hedge_ratio_bps"] == 6000 and nonces() == used  # nothing stored, nonce kept


def test_an_edit_that_trades_enough_or_stays_inside_the_band_is_accepted(api, fakechain):
    s = small_active(api, fakechain)
    assert patch(api, s["id"], {"hedge_ratio_bps": "6200"})[0].status_code == 200  # 3.3% gap: inside the band, no trade
    assert patch(api, s["id"], {"hedge_ratio_bps": "7000"})[0].status_code == 200  # 0.035: a $13 trade, viable


def test_a_full_close_to_zero_is_exempt_from_the_minimum(api, fakechain):
    st = Strategy(fund_id="f", market_id=M, market_symbol="XAU", hedge_ratio_bps=0, leverage=3, rebalance_band_bps=500,
                  target_exposure_units=D("0.2"), expected_amount_usd=D(1), size=D("-0.001"), margin_usd=D(10))
    service.require_order_viable(st, D(0), D(2650), "this rebalance")  # 0.001 oz = $2.65, but it only reduces to flat
    with pytest.raises(Exception) as e:
        service.require_order_viable(st, D("-0.002"), D(2650), "this rebalance")  # a $2.65 resize is not exempt
    assert e.value.code == "BAD_REQUEST"
