import signal
from decimal import Decimal

import pytest

from app.config import load_markets, settings
from app.errors import ServiceError
from app.mm import market_maker as mmod
from app.mm.market_maker import MMConfig, MarketMaker

D = Decimal
M = "XAU-HL"


def bare(cfg=None, **kw):
    mm = MarketMaker.__new__(MarketMaker)
    kw.setdefault("flatten_wait_s", 0)  # these tests do not exercise the startup wait (test_mm_startup.py does)
    mm.market, mm.cfg, mm.sz_dec, mm.addr, mm.mode = load_markets()[M], cfg or MMConfig(M, **kw), 4, "0xmm", "unifiedAccount"
    mm._stop = False
    return mm


class FakeExchange:
    def __init__(self):
        self.orders, self.cancelled, self.bulk = [], [], []

    def order(self, coin, is_buy, sz, px, otype, reduce_only=False):
        self.orders.append((coin, is_buy, sz, px, otype, reduce_only))
        return {"status": "ok"}

    def bulk_cancel(self, reqs):
        self.cancelled += reqs

    def bulk_orders(self, reqs):
        self.bulk.append(reqs)
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": i}} for i, _ in enumerate(reqs)]}}}


class FakeInfo:
    def __init__(self, inv="0", ctx=None, book=None):
        self.inv, self.ctx = D(inv), ctx or {"markPx": "4209.3", "oraclePx": "4160.4"}
        self.book = book or {"levels": [[{"px": "4201.1", "sz": "0.5"}], [{"px": "4222.1", "sz": "0.5"}]]}
        self.orders = [{"coin": "xyz:GOLD", "oid": 5, "side": "A", "limitPx": "9999", "sz": "1"}]  # stale: far from any quote

    def user_state(self, addr, dex=""):
        pos = [{"position": {"coin": "xyz:GOLD", "szi": str(self.inv)}}] if self.inv else []
        return {"assetPositions": pos, "marginSummary": {"accountValue": "10"}}

    def open_orders(self, addr, dex=""):
        return self.orders

    def post(self, path, body):
        if body["type"] == "l2Book":
            return self.book
        return {"universe": [{"name": "xyz:GOLD"}]}, [self.ctx]


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


def test_defaults_derive_inventory_limit_and_skew_and_validate_center():
    c = MMConfig(M, size=D("0.03"), levels=3, spread_bps=D(10))
    assert c.max_inventory == D("0.09") and c.skew_bps == 30 and c.center == "oracle"
    with pytest.raises(ServiceError):
        MMConfig(M, center="vwap")


def test_quote_ladder_flat_inventory_and_post_only_when_not_crossing():
    qs = bare(spread_bps=D(10), levels=3, size=D("0.03"), center="mark").quotes(D("4200"))
    assert sorted(q["limit_px"] for q in qs if q["is_buy"]) == [4187.4, 4191.6, 4195.8]
    assert sorted(q["limit_px"] for q in qs if not q["is_buy"]) == [4204.2, 4208.4, 4212.6]
    assert all(q["sz"] == 0.03 and q["order_type"] == {"limit": {"tif": "Alo"}} and not q["reduce_only"] for q in qs)


def test_inventory_skews_quotes_against_position():
    mm = bare(spread_bps=D(10), levels=1, size=D("0.03"))  # max_inventory 0.03, skew 10 bps
    flat = {q["is_buy"]: q["limit_px"] for q in mm.quotes(D("4000"))}
    long_ = {q["is_buy"]: q["limit_px"] for q in mm.quotes(D("4000"), D("0.015"))}  # half of max
    short = {q["is_buy"]: q["limit_px"] for q in mm.quotes(D("4000"), D("-0.015"))}
    assert long_[True] < flat[True] and long_[False] < flat[False]  # long: both sides lower (eager to sell)
    assert short[True] > flat[True] and short[False] > flat[False]  # short: both sides higher (eager to buy)


def test_side_that_adds_inventory_is_dropped_at_the_limit():
    mm = bare(spread_bps=D(10), levels=3, size=D("0.03"))
    at_long = mm.quotes(D("4000"), D("0.09"))
    assert at_long and not any(q["is_buy"] for q in at_long)
    at_short = mm.quotes(D("4000"), D("-0.2"))
    assert at_short and all(q["is_buy"] for q in at_short)


def test_crossing_quotes_use_gtc_to_take_stale_liquidity_toward_the_oracle():
    # oracle 4160 well below the 4201 bid: our asks cross it (GTC); our bids rest below everything (ALO)
    qs = bare(spread_bps=D(10), levels=3, size=D("0.03")).quotes(D("4160.4"), D(0), D("4201.1"), D("4222.1"))
    asks = [q for q in qs if not q["is_buy"]]
    bids = [q for q in qs if q["is_buy"]]
    assert all(q["order_type"] == {"limit": {"tif": "Gtc"}} for q in asks)  # 4164..4173 <= best bid 4201.1
    assert all(q["order_type"] == {"limit": {"tif": "Alo"}} for q in bids)


def test_center_price_sources(monkeypatch):
    mm = bare(center="oracle")
    mm.info = FakeInfo()
    assert mm.center_price() == D("4160.4")
    mm.cfg.center = "mark"
    assert mm.center_price() == D("4209.3")
    mm.cfg.center = "pyth"
    assert mm.center_price() == D(2650)  # conftest mock price


def test_tick_cancels_then_quotes_centered_on_oracle_with_inventory():
    mm = bare(spread_bps=D(10), levels=1, size=D("0.03"))
    mm.info, mm.exchange = FakeInfo(inv="-0.03"), FakeExchange()  # short at the limit: only bids allowed
    mm.tick()
    assert mm.exchange.cancelled == [{"coin": "xyz:GOLD", "oid": 5}]
    (sent,) = mm.exchange.bulk
    # max-short skew lifts the center by skew_bps (10) and the level-1 bid sits spread_bps (10) below it: back at the
    # oracle, i.e. a short MM bids at the oracle (not 10 bps under it, as it would when flat) to buy back eagerly
    assert [q["is_buy"] for q in sent] == [True] and sent[0]["limit_px"] == 4160.4


@pytest.mark.parametrize("inv,is_buy", [("-0.0015", True), ("0.0042", False)])
def test_flatten_is_reduce_only_ioc_in_the_right_direction(inv, is_buy, monkeypatch):
    monkeypatch.setattr(mmod.time, "sleep", lambda s: None)
    mm = bare()
    mm.info, mm.exchange = FakeInfo(inv=inv), FakeExchange()
    mm.flatten(attempts=1)
    coin, buy, sz, px, otype, reduce_only = mm.exchange.orders[0]
    assert (buy, sz, otype, reduce_only) == (is_buy, float(abs(D(inv))), {"limit": {"tif": "Ioc"}}, True)
    assert (px > 4209.3) == is_buy  # buys priced through the mark, sells below it


def test_flatten_does_nothing_when_flat_and_reports_residual(monkeypatch):
    monkeypatch.setattr(mmod.time, "sleep", lambda s: None)
    mm = bare()
    mm.info, mm.exchange = FakeInfo(inv="0"), FakeExchange()
    assert mm.flatten() == 0 and mm.exchange.orders == []
    mm.info = FakeInfo(inv="-0.0015")  # fake never fills, so the residual remains
    assert mm.flatten(attempts=2) == D("-0.0015") and len(mm.exchange.orders) == 2


def test_run_shutdown_cancels_then_flattens_and_clears_pidfile(monkeypatch, tmp_path):
    monkeypatch.setattr(mmod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mmod, "pidfile", lambda m: tmp_path / "mm.pid")
    mm = bare()
    mm.info, mm.exchange = FakeInfo(inv="-0.0015"), FakeExchange()
    calls = []
    mm.tick = lambda: (calls.append("tick"), mm.stop())  # one tick, then a stop request (as `mm stop` does)
    mm.run()
    assert calls == ["tick"] and mm.exchange.cancelled and mm.exchange.orders[0][5] is True
    assert not (tmp_path / "mm.pid").exists()


def test_ctrl_c_also_shuts_down_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(mmod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mmod, "pidfile", lambda m: tmp_path / "mm.pid")
    mm = bare()
    mm.info, mm.exchange = FakeInfo(inv="0.01"), FakeExchange()

    def boom():
        raise KeyboardInterrupt

    mm.tick = boom
    mm.run()
    assert mm.exchange.cancelled and mm.exchange.orders[0][1] is False  # long -> sell reduce-only


def test_request_stop_signals_pid_or_reports_none(monkeypatch, tmp_path):
    monkeypatch.setattr(mmod, "pidfile", lambda m: tmp_path / "mm.pid")
    assert mmod.request_stop(M) is False
    (tmp_path / "mm.pid").write_text("4242")
    sent = []
    monkeypatch.setattr(mmod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    assert mmod.request_stop(M) is True and sent == [(4242, signal.SIGTERM)]
    monkeypatch.setattr(mmod.os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))
    assert mmod.request_stop(M) is False and not (tmp_path / "mm.pid").exists()  # stale pid cleaned up


# ---- quota: only send actions when quotes have drifted ---------------------------------------

def resting_like(mm, qs):
    return [{"coin": "xyz:GOLD", "oid": i, "side": "B" if q["is_buy"] else "A", "limitPx": str(q["limit_px"]), "sz": str(q["sz"])}
            for i, q in enumerate(qs)]


def settled_mm(**kw):
    mm = bare(spread_bps=D(10), levels=2, size=D("0.03"), **kw)
    mm.info, mm.exchange = FakeInfo(), FakeExchange()
    mm.info.orders = resting_like(mm, mm.quotes(D("4160.4"), D(0), D("4201.1"), D("4222.1")))
    return mm


def test_tick_sends_nothing_while_resting_quotes_are_still_right():
    mm = settled_mm()
    for _ in range(5):
        mm.tick()
    assert mm.exchange.bulk == [] and mm.exchange.cancelled == [] and mm.requests == 0


def test_tick_leaves_a_small_drift_alone_but_requotes_a_big_one():
    mm = settled_mm()
    mm.info.ctx = {"markPx": "4209.3", "oraclePx": "4160.9"}  # +1.2 bps: inside requote_bps (3)
    mm.tick()
    assert mm.exchange.bulk == []
    mm.info.ctx = {"markPx": "4209.3", "oraclePx": "4165.0"}  # +11 bps
    mm.tick()
    assert len(mm.exchange.bulk) == 1 and len(mm.exchange.cancelled) == 4 and mm.requests == 2


def test_tick_requotes_when_a_quote_was_filled_or_inventory_changes_the_skew():
    mm = settled_mm()
    mm.info.orders = mm.info.orders[1:]  # one level got hit
    mm.tick()
    assert len(mm.exchange.bulk) == 1
    mm = settled_mm()
    mm.info.inv = D("0.06")  # the long side is now dropped at the limit
    mm.tick()
    assert len(mm.exchange.bulk) == 1


def test_rate_limit_pauses_quoting_with_one_clear_message_and_resumes(caplog, monkeypatch):
    mm = settled_mm(rate_limit_backoff_s=300)
    mm.info.ctx = {"markPx": "4209.3", "oraclePx": "4300"}
    limited = {"status": "err", "response": "Too many cumulative requests sent (12259 > 11939) for cumulative volume traded $1940.55"}
    mm.exchange.bulk_orders = lambda reqs: mm.exchange.bulk.append(reqs) or limited
    with caplog.at_level("ERROR", logger="sereel.mm"):
        mm.tick()
        mm.tick()
        mm.tick()
    assert len(mm.exchange.bulk) == 1  # paused after the first refusal: no more requests burned
    (msg,) = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert "rate-limited" in msg and "trading taker volume" in msg and "fresh account" in msg
    monkeypatch.setattr(mmod.time, "time", lambda: mm._paused_until + 1)
    mm.tick()
    assert len(mm.exchange.bulk) == 2  # retried after the pause
