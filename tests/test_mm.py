from decimal import Decimal

import pytest

from app.config import load_markets, settings
from app.errors import ServiceError
from app.mm.market_maker import MMConfig, MarketMaker

D = Decimal
M = "XAU-HL"


def test_refuses_mainnet_even_with_allow_mainnet(monkeypatch):
    monkeypatch.setattr(settings, "hl_api_url", "https://api.hyperliquid.xyz")
    monkeypatch.setattr(settings, "allow_mainnet", True)
    with pytest.raises(ServiceError) as e:
        MarketMaker(load_markets()[M], MMConfig(M))
    assert e.value.code == "MM_MAINNET_REFUSED"


@pytest.mark.parametrize("size", ["0.01", "0.06", "1"])
def test_size_bounds_enforced(size):
    with pytest.raises(ServiceError) as e:
        MMConfig(M, size=D(size))
    assert e.value.code == "MM_SIZE_OUT_OF_RANGE"


@pytest.mark.parametrize("size", ["0.02", "0.03", "0.05"])
def test_size_bounds_inclusive(size):
    assert MMConfig(M, size=D(size)).size == D(size)


def test_quote_ladder_is_symmetric_post_only_and_sized():
    mm = MarketMaker.__new__(MarketMaker)
    mm.market, mm.cfg, mm.sz_dec = load_markets()[M], MMConfig(M, spread_bps=D(10), levels=3, size=D("0.03")), 4
    qs = mm.quotes(D("4200"))
    bids = sorted(q["limit_px"] for q in qs if q["is_buy"])
    asks = sorted(q["limit_px"] for q in qs if not q["is_buy"])
    assert bids == [4187.4, 4191.6, 4195.8]  # 30 / 20 / 10 bps below the mark
    assert asks == [4204.2, 4208.4, 4212.6]  # 10 / 20 / 30 bps above
    assert all(q["sz"] == 0.03 and q["order_type"] == {"limit": {"tif": "Alo"}} and not q["reduce_only"] for q in qs)
