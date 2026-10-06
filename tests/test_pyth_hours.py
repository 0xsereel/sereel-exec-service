import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from zoneinfo import ZoneInfo

from app import pyth
from app.config import settings
from app.errors import ServiceError

# the real XAU/USD schedule as served by Hermes
XAU = ("America/New_York;0000-1700&1800-2400,0000-1700&1800-2400,0000-1700&1800-2400,0000-1700&1800-2400,0000-1700,C,"
       "1800-2400;0907/0000-1430&1800-2400,1126/0000-1430&1800-2400,1127/0000-1445,1224/0000-1345,1225/C,"
       "1231/0000-1700,0101/C,0118/0000-1430&1800-2400,0215/0000-1430&1800-2400,0325/0000-1700,0326/C,"
       "0531/0000-1430&1800-2400,0618/0000-1300,0705/0000-1430&1800-2400")
FID = "ab" * 32


def ny(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)


@pytest.mark.parametrize("when,expected", [
    (ny(2026, 10, 5, 12), True),    # Monday noon
    (ny(2026, 10, 5, 17, 30), False),  # Monday: the daily 17:00-18:00 break
    (ny(2026, 10, 5, 18), True),
    (ny(2026, 10, 9, 16, 59), True),   # Friday just before the close
    (ny(2026, 10, 9, 17), False),      # Friday close
    (ny(2026, 10, 9, 20), False),      # Friday evening: no evening session
    (ny(2026, 10, 10, 12), False),     # Saturday: closed all day
    (ny(2026, 10, 11, 17, 59), False),  # Sunday before the open
    (ny(2026, 10, 11, 18), True),      # Sunday 18:00 reopen
    (ny(2026, 12, 25, 12), False),     # Christmas: holiday override "C"
    (ny(2026, 11, 27, 15), False),     # holiday override 0000-1445
    (ny(2026, 11, 27, 14), True),
    (ny(2026, 7, 6, 12), True),        # DST period: tz conversion must follow New York time
])
def test_schedule_open_and_closed(when, expected):
    assert pyth.is_open(XAU, when) is expected


def hermes(monkeypatch, age_s, schedule=XAU):
    monkeypatch.setattr(settings, "pyth_mock_price", None)
    pyth._schedules.clear()

    def fake_get(url, params=None, headers=None, timeout=None):
        if url.endswith("/v2/price_feeds"):
            return httpx.Response(200, json=[{"id": FID, "attributes": {"schedule": schedule}}], request=httpx.Request("GET", url))
        body = {"parsed": [{"price": {"price": "415000", "expo": -2, "publish_time": int(time.time()) - age_s}}]}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)


def at(monkeypatch, when):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return when.astimezone(tz) if tz else when

    monkeypatch.setattr(pyth, "datetime", FakeDT)


def test_stale_price_while_the_market_is_closed_returns_last_price_flagged(monkeypatch):
    hermes(monkeypatch, age_s=40 * 3600)  # since Friday's close
    at(monkeypatch, ny(2026, 10, 10, 12))  # Saturday
    p = pyth.get_price(FID, 30, symbol="XAU")
    assert p.market_closed is True and p.price == Decimal("4150.00")  # no STALE_PRICE


def test_stale_price_while_the_market_is_open_is_still_stale_price(monkeypatch):
    hermes(monkeypatch, age_s=600)
    at(monkeypatch, ny(2026, 10, 5, 12))  # Monday noon: it should be publishing
    with pytest.raises(ServiceError) as e:
        pyth.get_price(FID, 30, symbol="XAU")
    assert e.value.code == "STALE_PRICE"


def test_fresh_price_is_never_flagged_closed(monkeypatch):
    hermes(monkeypatch, age_s=2)
    at(monkeypatch, ny(2026, 10, 10, 12))
    assert pyth.get_price(FID, 30, symbol="XAU").market_closed is False


def test_unknown_schedule_or_no_symbol_keeps_the_strict_behaviour(monkeypatch):
    hermes(monkeypatch, age_s=40 * 3600, schedule=None)
    at(monkeypatch, ny(2026, 10, 10, 12))
    with pytest.raises(ServiceError) as e:
        pyth.get_price(FID, 30, symbol="XAU")
    assert e.value.code == "STALE_PRICE"
    hermes(monkeypatch, age_s=40 * 3600)
    with pytest.raises(ServiceError):
        pyth.get_price(FID, 30)  # callers that do not pass a symbol never get the closed-market relaxation


def test_schedule_is_cached(monkeypatch):
    hermes(monkeypatch, age_s=2)
    calls = []
    real = httpx.get

    def counting(url, **k):
        calls.append(url)
        return real(url, **k)

    monkeypatch.setattr(httpx, "get", counting)
    pyth._schedules.clear()
    for _ in range(3):
        pyth.feed_schedule(FID, "XAU")
    assert len([c for c in calls if c.endswith("/v2/price_feeds")]) == 1
