"""GET /markets must carry `status`: the frontend's StrategyMarket.status is 'active' | 'coming_soon', compared strictly
(`market.status !== 'active'` disables the market). A missing field is therefore 'coming soon', which is what XAU showed."""
import pytest

from app.config import load_markets
from app import main
from app.state import state
from test_strategies import BODY, M


@pytest.fixture(autouse=True)
def fresh_cache():
    main._market_cache.clear()


def row(api):
    (m,) = api.get("/markets").json()
    return m


def test_the_shipped_xau_market_is_exactly_the_string_active(api):
    assert load_markets()[M].status == "active"
    m = row(api)
    assert "status" in m and m["status"] == "active" and type(m["status"]) is str  # exact, lowercase, a string
    assert m["status"] in ("active", "coming_soon")


def test_every_row_has_a_status(api):
    for m in api.get("/markets").json():
        assert m["status"] in ("active", "coming_soon")


def test_a_disabled_market_is_coming_soon_and_cannot_take_new_strategies(api, monkeypatch):
    off = state.markets[M].model_copy(update={"enabled": False})
    monkeypatch.setitem(state.markets, M, off)
    assert row(api)["status"] == "coming_soon"
    r = api.post("/strategies", json=BODY)
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and "coming_soon" in r.json()["error"]
    assert set(r.json()) == {"error", "code"}


def test_status_comes_from_configuration_not_from_live_prices(api, monkeypatch):
    """A slow or failing Pyth/Hyperliquid must not grey the market out in the UI."""
    row(api)  # a good read first, so there is a last good price to serve
    monkeypatch.setattr(state.venue, "mark_price", lambda m: (_ for _ in ()).throw(RuntimeError("venue down")))
    m = row(api)
    assert m["status"] == "active" and m["price_stale"] is True and "venue down" in m["error"]


def test_a_closed_market_is_still_active(api, monkeypatch):
    from app import pyth
    from decimal import Decimal

    monkeypatch.setattr(pyth, "get_price", lambda *a, **k: pyth.PythPrice(Decimal(2650), 0, "f", market_closed=True))
    m = row(api)
    assert m["market_closed"] is True and m["status"] == "active"  # closed hours are a flag, not "coming soon"


def test_the_yaml_switch_is_read(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text("- {market_id: A, symbol: A, hl_coin: 'x:A', pyth_feed_id: ab, enabled: false}\n- {market_id: B, symbol: B, hl_coin: 'x:B', pyth_feed_id: cd}\n")
    ms = load_markets(f)
    assert (ms["A"].status, ms["B"].status) == ("coming_soon", "active")  # enabled defaults to true


def test_the_endpoint_still_needs_the_api_key(api):
    assert api.get("/markets", headers={"X-Sereel-Key": "wrong"}).status_code == 401
