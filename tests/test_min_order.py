"""The venue rejects orders under $10 notional: a strategy whose hedge is smaller is refused up front, with the numbers."""
from decimal import Decimal

from app.config import settings
from test_step1 import M, set_price
from test_strategies import BODY, create

D = Decimal


def post(api, **over):
    return api.post("/strategies", json={**BODY, **over})


def test_a_hedge_under_the_venue_minimum_is_refused_before_anything_is_stored(api, fakechain):
    set_price(4000)
    before = len(api.get("/strategies").json())
    r = post(api, target_exposure_units=0.002, hedge_ratio_bps=10000, expected_amount_usd=5)  # an $8 order
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and set(r.json()) == {"error", "code"}
    e = r.json()["error"]
    assert "below the venue's $10 minimum order" in e and "about $8.00" in e and "at least 0.0027" in e
    assert len(api.get("/strategies").json()) == before


def test_the_message_says_how_much_exposure_is_enough_and_that_amount_is_accepted(api, fakechain):
    set_price(4000)
    r = post(api, target_exposure_units=0.001, hedge_ratio_bps=10000, expected_amount_usd=50)
    assert r.status_code == 400
    need = D(r.json()["error"].split("at least ")[1].split(" ")[0])
    assert need * 4000 >= D("10.5")
    ok = post(api, target_exposure_units=float(need), hedge_ratio_bps=10000, expected_amount_usd=50)
    assert ok.status_code == 200, ok.text


def test_the_minimum_applies_to_the_hedged_size_not_the_exposure(api, fakechain):
    set_price(4000)  # exposure 0.01 (= $40) but only 10% hedged: a $4 order
    r = post(api, target_exposure_units=0.01, hedge_ratio_bps=1000, expected_amount_usd=50)
    assert r.status_code == 400 and "below the venue's $10 minimum order" in r.json()["error"]


def test_a_zero_hedge_has_no_order_so_no_minimum(api, fakechain):
    set_price(4000)
    assert post(api, hedge_ratio_bps=0, expected_amount_usd=1).status_code == 200


def test_the_minimum_is_configurable(api, fakechain, monkeypatch):
    set_price(4000)
    monkeypatch.setattr(settings, "min_order_usd", D(1))
    assert post(api, target_exposure_units=0.001, hedge_ratio_bps=10000, expected_amount_usd=5).status_code == 200
