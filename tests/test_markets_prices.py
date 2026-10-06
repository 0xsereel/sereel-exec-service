"""GET /markets and the frontend's StrategyMarket type:

    { market_id: string, symbol: string, venue_coin: string, max_leverage: number,
      mark_price_usd: number, pyth_price_usd: number, market_closed: boolean, deviation_bps: number }

Every price is a NON-NULL number and market_closed a boolean. A failed price read used to put `null` there, which crashes or hangs
the UI. Now it serves the last good values (price_stale) or, with none, fails the whole call with a clear error.
"""
import time
from decimal import Decimal

import pytest

from app import main, pyth
from app.errors import ServiceError
from app.state import state

FRONTEND_FIELDS = {"market_id": str, "symbol": str, "venue_coin": str, "max_leverage": (int, float), "mark_price_usd": float,
                   "pyth_price_usd": float, "market_closed": bool, "deviation_bps": (int, float)}


@pytest.fixture(autouse=True)
def fresh_cache():
    main._market_cache.clear()


def markets(api):
    return api.get("/markets")


def down(monkeypatch, exc=RuntimeError("venue down")):
    monkeypatch.setattr(state.venue, "mark_price", lambda m: (_ for _ in ()).throw(exc))


def test_every_row_matches_the_frontends_type_exactly(api):
    (m,) = markets(api).json()
    for field, typ in FRONTEND_FIELDS.items():
        assert field in m and isinstance(m[field], typ) and m[field] is not None, (field, m.get(field))
    assert type(m["market_closed"]) is bool and m["price_stale"] is False and "error" not in m


def test_a_failed_read_serves_the_last_good_prices_flagged_stale(api, monkeypatch):
    good = markets(api).json()[0]
    down(monkeypatch)
    r = markets(api)
    assert r.status_code == 200
    (m,) = r.json()
    assert m["price_stale"] is True and "venue down" in m["error"]
    for field in ("mark_price_usd", "pyth_price_usd", "deviation_bps", "market_closed"):
        assert m[field] == good[field]  # the last good values, never null
    for field, typ in FRONTEND_FIELDS.items():
        assert isinstance(m[field], typ) and m[field] is not None, field


def test_a_pyth_failure_is_handled_the_same_way(api, monkeypatch):
    markets(api)

    def bad(*a, **k):
        raise ServiceError("PRICE_SOURCE_AUTH", "Pyth rejected the credentials", 502)

    monkeypatch.setattr(pyth, "get_price", bad)
    (m,) = markets(api).json()
    assert m["price_stale"] is True and "Pyth rejected" in m["error"] and m["pyth_price_usd"] == 2650.0


def test_recovery_clears_the_stale_flag(api, monkeypatch):
    markets(api)
    with monkeypatch.context() as mp:
        down(mp)
        assert markets(api).json()[0]["price_stale"] is True
    m = markets(api).json()[0]
    assert m["price_stale"] is False and "error" not in m


def test_with_no_good_prices_ever_it_is_a_clear_error_not_a_null_row(api, monkeypatch):
    down(monkeypatch)
    r = markets(api)
    assert r.status_code == 503 and set(r.json()) == {"error", "code"} and r.json()["code"] == "VENUE_UNAVAILABLE"
    assert "no market price is available" in r.json()["error"] and "venue down" in r.json()["error"]


def test_the_underlying_error_code_is_kept_when_there_is_one(api, monkeypatch):
    monkeypatch.setattr(pyth, "get_price", lambda *a, **k: (_ for _ in ()).throw(ServiceError("STALE_PRICE", "Pyth price is 600s old", 503)))
    r = markets(api)
    assert r.status_code == 503 and r.json()["code"] == "STALE_PRICE"  # a code Cantina already handles


def test_old_cached_prices_are_not_served_forever(api, monkeypatch):
    markets(api)
    ts, prices = main._market_cache["XAU-HL"]
    main._market_cache["XAU-HL"] = (ts - main.MARKET_CACHE_MAX_AGE_S - 1, prices)
    down(monkeypatch)
    assert markets(api).status_code == 503  # too old to trust


def test_no_row_ever_contains_a_null(api, monkeypatch):
    markets(api)
    down(monkeypatch)
    for r in markets(api).json():
        assert all(v is not None for v in r.values()), r


def test_the_extra_fields_are_additive(api):
    (m,) = markets(api).json()
    assert {"status", "price_stale"} <= set(m) and set(FRONTEND_FIELDS) <= set(m)  # a type that ignores unknown keys is unaffected
