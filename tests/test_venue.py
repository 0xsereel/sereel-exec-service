import threading
import time
from decimal import Decimal

import pytest

from app import pyth
from app.config import load_markets
from app.errors import ServiceError
from app.venue.base import CctpHyperliquidRoute, MirroredRoute
from app.venue.simulated import SimulatedVenue

M = "XAU-HL"
D = Decimal


def venue(**kw):
    return SimulatedVenue(load_markets(), **kw)


def test_full_fill_and_delta_trading():
    v = venue()
    r = v.set_position("s1", M, D("-0.6"))
    assert r.filled == D("-0.6") and r.remaining == 0 and r.oids and r.fee > 0 and r.avg_px == D(2650)
    r2 = v.set_position("s1", M, D("-0.2"), current_size=D("-0.6"))
    assert r2.filled == D("0.4") and v.position(None, M).size == D("-0.2")


def test_no_trade_when_already_at_target():
    v = venue()
    r = v.set_position("s1", M, D("-1"), current_size=D("-1"))
    assert r.filled == 0 and r.remaining == 0 and not r.oids and v.position(None, M).size == 0


def test_partial_fills_report_remaining_gap():
    v = venue(fill_fraction=D("0.5"))
    r = v.set_position("s1", M, D("-1"))
    assert len(r.partials) == 3  # IOC_MAX_RETRIES
    assert [p.filled for p in r.partials] == [D("-0.5"), D("-0.25"), D("-0.125")]
    assert r.filled == D("-0.875") and r.remaining == D("-0.125")


def test_no_fill_raises():
    with pytest.raises(ServiceError) as e:
        venue(fill_fraction=D(0)).set_position("s1", M, D("-1"))
    assert e.value.code == "ORDER_NOT_FILLED"


def test_price_deviation_rejected_before_any_order():
    v = venue()
    v.price_override[M] = D("3000")  # Pyth mock says 2650 => ~1320 bps
    with pytest.raises(ServiceError) as e:
        v.set_position("s1", M, D("-1"))
    assert e.value.code == "PRICE_DEVIATION" and v.position(None, M).size == 0


def test_stale_pyth_rejected_before_any_order(monkeypatch):
    def stale(*a, **k):
        raise ServiceError("STALE_PRICE", "old", 503)

    monkeypatch.setattr(pyth, "get_price", stale)
    v = venue()
    v.price_override[M] = D("2650")
    with pytest.raises(ServiceError) as e:
        v.set_position("s1", M, D("-1"))
    assert e.value.code == "STALE_PRICE" and v.position(None, M).size == 0


def test_unknown_market():
    with pytest.raises(ServiceError) as e:
        venue().mark_price("NOPE")
    assert e.value.code == "UNKNOWN_MARKET"


def test_insufficient_margin():
    v = venue(funds=D(100))
    v.ensure_margin(M, D(60))
    v.ensure_margin(M, D(60))  # idempotent at the same level
    assert v.funds == 40
    with pytest.raises(ServiceError) as e:
        v.ensure_margin(M, D(500))
    assert e.value.code == "INSUFFICIENT_MARGIN" and v.margin_balance(M) == 60


def test_short_profit_realized_and_unrealized():
    v = venue()
    v.ensure_margin(M, D(1000))
    r = v.set_position("s1", M, D("-1"))
    v.price_override[M] = D("2640")  # within the deviation band of the 2650 Pyth mock
    assert v.position(None, M).unrealized_pnl == D(10)
    v.set_position("s1", M, D(0), current_size=D("-1"))
    p = v.position(None, M)
    assert p.size == 0 and p.entry_px == 0 and p.unrealized_pnl == 0
    open_fee, close_fee = r.fee, D("1") * D("2640") * D("0.00045")
    assert p.account_value == D(1000) + 10 - open_fee - close_fee  # realized +10 less both fees


def test_flip_through_zero_realizes_closed_part_and_resets_entry():
    v = venue()
    v.ensure_margin(M, D(1000))
    v.set_position("s1", M, D("-1"))
    v.price_override[M] = D("2640")
    v.set_position("s1", M, D("0.5"), current_size=D("-1"))  # buy 1.5: close 1 short (+10), open 0.5 long
    p = v.position(None, M)
    assert p.size == D("0.5") and p.entry_px == D(2640)
    assert v._cash[M] == D(1000) + 10 - D("1") * D("2650") * D("0.00045") - D("1.5") * D("2640") * D("0.00045")


def test_concurrent_strategies_share_position_without_double_count():
    v = venue()
    orig = v._ioc

    def slow_ioc(*a, **k):  # widen the race window; without the per-account lock this over-trades badly
        out = orig(*a, **k)
        time.sleep(0.005)
        return out

    v._ioc = slow_ioc
    results = {}

    def go(sid):
        results[sid] = v.set_position(sid, M, D("-1"))

    ts = [threading.Thread(target=go, args=(f"s{i}",)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert v.position(None, M).size == D(-8)
    assert sum(r.filled for r in results.values()) == D(-8)  # attribution == position delta
    assert all(r.filled == D(-1) for r in results.values())


def test_funding_routes():
    assert MirroredRoute().to_venue(D(5), "r")["route"] == "mirrored"
    with pytest.raises(NotImplementedError):
        CctpHyperliquidRoute().to_venue(D(5), "r")
