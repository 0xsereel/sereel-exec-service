"""HyperliquidVenue logic against fake SDK objects (no network)."""
from decimal import Decimal

import pytest

from app.config import load_markets
from app.errors import ServiceError
from app.venue.hyperliquid import HyperliquidVenue

D = Decimal
M = "XAU-HL"


class FakeInfo:
    def __init__(self):
        self.dex_value = "100.0"
        self.main_withdrawable = "500.0"
        self.spot = "300.0"

    def user_state(self, addr, dex=""):
        if dex == "":
            return {"withdrawable": self.main_withdrawable, "marginSummary": {"accountValue": "500", "totalMarginUsed": "0"},
                    "assetPositions": []}
        return {"marginSummary": {"accountValue": self.dex_value, "totalMarginUsed": "10"}, "assetPositions": [
            {"position": {"coin": "xyz:GOLD", "szi": "-0.6", "entryPx": "2650.0", "unrealizedPnl": "5.5", "liquidationPx": "3900"}}]}

    def post(self, path, body):
        return {"universe": [{"name": "xyz:GOLD"}]}, [{"markPx": "2651.5"}]

    def spot_user_state(self, addr):
        return {"balances": [{"coin": "USDC", "total": self.spot, "hold": "0.0"}]}

    def spot_meta(self):
        return {"tokens": [{"name": "USDC", "tokenId": "0xabc"}]}

    def user_fills(self, addr):
        return [{"oid": 1, "fee": "0.4"}, {"oid": 1, "fee": "0.1"}, {"oid": 2, "fee": "9"}]

    def user_funding_history(self, addr, since):
        return [{"delta": {"coin": "xyz:GOLD", "usdc": "-0.25"}}, {"delta": {"coin": "xyz:GOLD", "usdc": "0.05"}},
                {"delta": {"coin": "BTC", "usdc": "99"}}]


class FakeExchange:
    def __init__(self, status):
        self.status, self.calls, self.cancelled, self.sent = status, [], [], []

    def order(self, coin, is_buy, sz, px, otype):
        self.calls.append((coin, is_buy, sz, px, otype))
        return {"status": "ok", "response": {"data": {"statuses": [self.status]}}}

    def cancel(self, coin, oid):
        self.cancelled.append(oid)

    def send_asset(self, dest, src, dst, token, amount):
        self.sent.append((dest, src, dst, token, amount))
        return {"status": "ok"}


def venue(status=None, master=True):
    v = HyperliquidVenue.__new__(HyperliquidVenue)
    v.markets, v.master, v.account_key = load_markets(), "0xme", "0xme"
    v.info, v._sz_dec, v.asset_ids = FakeInfo(), {M: 4}, {M: 750003}
    v.exchange = FakeExchange(status or {"filled": {"avgPx": "2650", "oid": 7, "totalSz": "0.1"}})
    v._master_exchange = FakeExchange({}) if master else None
    return v


def test_price_rounding_five_sig_figs_and_decimal_cap():
    r = HyperliquidVenue._round_px
    assert r(D("2663.2517"), 4) == 2663.3  # 5 sig figs, and <= 6-4 = 2 decimals
    assert r(D("12.345678"), 2) == 12.346


def test_position_and_mark_parsing():
    p = venue().position(None, M)
    assert (p.size, p.entry_px, p.unrealized_pnl, p.liquidation_px, p.mark, p.account_value) == \
           (D("-0.6"), D(2650), D("5.5"), D(3900), D("2651.5"), D(100))


def test_funding_sums_only_this_coin():
    assert venue().funding_since(M, 0) == D("-0.20")


def test_fees_use_actual_fills_for_the_orders_only():
    assert venue()._fees_for(M, ["1"], D(1000)) == D("0.5")


def test_ioc_filled_sends_ioc_with_rounded_price():
    v = venue()
    assert v._ioc(M, False, D("0.6"), D("2637.2518")) == (D(2650), "7")
    coin, is_buy, sz, px, otype = v.exchange.calls[0]
    assert (coin, is_buy, sz, px, otype) == ("xyz:GOLD", False, 0.6, 2637.3, {"limit": {"tif": "Ioc"}})


def test_ioc_no_cross_is_zero_fill_not_error():
    v = venue({"error": "Order could not immediately match against any resting orders. asset=750003"})
    assert v._ioc(M, False, D(1), D(2600)) == (None, None)


def test_ioc_resting_is_cancelled():
    v = venue({"resting": {"oid": 9}})
    assert v._ioc(M, False, D(1), D(2600)) == (None, "9") and v.exchange.cancelled == [9]


def test_ioc_margin_error_maps_to_insufficient_margin():
    with pytest.raises(ServiceError) as e:
        venue({"error": "Insufficient margin to place order."})._ioc(M, False, D(1), D(2600))
    assert e.value.code == "INSUFFICIENT_MARGIN"


def test_ioc_other_error_is_order_not_filled():
    with pytest.raises(ServiceError) as e:
        venue({"error": "Price must be divisible by tick size."})._ioc(M, False, D(1), D(2600))
    assert e.value.code == "ORDER_NOT_FILLED"


def test_ensure_margin_tops_up_only_the_shortfall_via_master():
    v = venue()
    v.ensure_margin(M, D(160))  # dex holds 100 -> move 60 from main perp to xyz
    assert v._master_exchange.sent == [("0xme", "", "xyz", "USDC:0xabc", 60.0)] and not v.exchange.sent
    v._master_exchange.sent.clear()
    v.ensure_margin(M, D(80))
    assert v._master_exchange.sent == []  # already covered


def test_ensure_margin_draws_main_then_spot():
    v = venue()
    v.info.main_withdrawable = "20.0"
    v.ensure_margin(M, D(160))  # needs 60: 20 from main perp, 40 from spot
    assert v._master_exchange.sent == [("0xme", "", "xyz", "USDC:0xabc", 20.0), ("0xme", "spot", "xyz", "USDC:0xabc", 40.0)]


def test_ensure_margin_spot_only_like_the_real_account():
    v = venue()
    v.info.main_withdrawable = "0.0"
    v.ensure_margin(M, D(160))
    assert v._master_exchange.sent == [("0xme", "spot", "xyz", "USDC:0xabc", 60.0)]


def test_ensure_margin_errors():
    with pytest.raises(ServiceError) as e:
        venue().ensure_margin(M, D(1000))  # needs 900, main 500 + spot 300
    assert e.value.code == "INSUFFICIENT_MARGIN" and "spot USDC is 300" in e.value.message
    with pytest.raises(ServiceError) as e:
        venue(master=False).ensure_margin(M, D(160))
    assert e.value.code == "INSUFFICIENT_MARGIN" and "master key" in e.value.message


def test_release_margin_moves_dex_back_to_main():
    v = venue()
    v.release_margin(M, D(25))
    assert v._master_exchange.sent == [("0xme", "xyz", "", "USDC:0xabc", 25.0)]
