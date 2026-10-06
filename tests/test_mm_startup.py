"""Market maker startup: leftover inventory is flattened (reduce-only) before normal quoting."""
from decimal import Decimal

import pytest

from app.mm import market_maker as mmod
from app.mm.market_maker import MMConfig
from test_mm import FakeExchange, FakeInfo, bare

D = Decimal


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Book(FakeInfo):
    """FakeInfo with a settable book and an exchange that can fill reduce-only orders."""

    def __init__(self, inv="0", bids=(), asks=()):
        super().__init__(inv=inv)
        self.bids, self.asks = list(bids), list(asks)
        self.polls = 0

    def post(self, path, body):
        if body["type"] == "l2Book":
            self.polls += 1
            return {"levels": [[{"px": px, "sz": sz} for px, sz in self.bids], [{"px": px, "sz": sz} for px, sz in self.asks]]}
        return super().post(path, body)


class Exch(FakeExchange):
    def __init__(self, info, fill=True):
        super().__init__()
        self.info, self.fill, self.events = info, fill, []

    def order(self, coin, is_buy, sz, px, otype, reduce_only=False):
        self.events.append(("order", is_buy, sz, reduce_only))
        out = super().order(coin, is_buy, sz, px, otype, reduce_only)
        if self.fill and reduce_only:
            self.info.inv = D(0)
        return out

    def bulk_orders(self, reqs):
        self.events.append(("quotes", len(reqs), self.info.inv))
        return super().bulk_orders(reqs)

    def bulk_cancel(self, reqs):
        self.events.append(("cancel", len(reqs)))
        return super().bulk_cancel(reqs)


@pytest.fixture()
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mmod.time, "time", c)
    monkeypatch.setattr(mmod.time, "sleep", c.sleep)
    return c


def mm_with(info, clock, wait=60, **kw):
    mm = bare(flatten_wait_s=wait, **kw)
    mm.info, mm.exchange = info, Exch(info)
    mm._sleep = clock.sleep
    return mm


def test_leftover_short_is_bought_back_reduce_only_before_any_quote(clock, monkeypatch, tmp_path):
    monkeypatch.setattr(mmod, "pidfile", lambda m: tmp_path / "pid")
    info = Book(inv="-0.0334", asks=[("4160.0", "0.2")])  # the book can take the buy-back
    mm = mm_with(info, clock)
    mm.tick = lambda: (mm.exchange.events.append(("tick", info.inv)), mm.stop())
    mm.run()
    kinds = [e[0] for e in mm.exchange.events]
    assert kinds.index("order") < kinds.index("tick")  # flattened first
    order = next(e for e in mm.exchange.events if e[0] == "order")
    assert order[1] is True and order[2] == 0.0334 and order[3] is True  # buy, whole size, reduce-only
    assert next(e for e in mm.exchange.events if e[0] == "tick")[1] == 0  # quoting began flat
    assert kinds[0] == "cancel"  # stale quotes from the previous run were cancelled first


def test_leftover_long_is_sold_into_the_bids(clock):
    info = Book(inv="0.014", bids=[("4200.0", "0.1")])  # within 1% of the 4209 mark
    mm = mm_with(info, clock)
    assert mm.flatten_on_start() == 0
    (order,) = [e for e in mm.exchange.events if e[0] == "order"]
    assert order[1] is False and order[2] == 0.014 and order[3] is True


def test_the_side_checked_is_the_one_the_flatten_needs(clock):
    info = Book(inv="-0.02", bids=[("4150.0", "9")], asks=[])  # plenty of bids but no asks: a short cannot be bought back
    mm = mm_with(info, clock, wait=10)
    assert mm.flatten_on_start() == D("-0.02")
    assert [e for e in mm.exchange.events if e[0] == "order"] == []  # no order into a book with nothing to buy from


def test_it_waits_for_the_book_then_flattens_and_only_then_quotes(clock):
    info = Book(inv="-0.0334")
    mm = mm_with(info, clock, wait=60)
    sleeps = []
    mm._sleep = lambda s: (sleeps.append(s), clock.sleep(s), setattr(info, "asks", [("4160.0", "0.2")]) if len(sleeps) == 3 else None)
    assert mm.flatten_on_start() == 0
    assert len(sleeps) == 3 and info.inv == 0  # polled while empty, then the book appeared and it flattened (no extra wait after)
    assert [e[0] for e in mm.exchange.events].count("order") == 1


def test_a_book_that_never_allows_it_times_out_and_quoting_starts_skewed(clock, caplog):
    import logging

    info = Book(inv="-0.0334")
    mm = mm_with(info, clock, wait=20)
    caplog.set_level(logging.WARNING, logger="sereel.mm")
    left = mm.flatten_on_start()
    assert left == D("-0.0334") and clock.t - 1000.0 >= 20  # waited the whole grace period, no more than a tick over
    assert clock.t - 1000.0 < 20 + mm.cfg.refresh_s + 1
    assert [e for e in mm.exchange.events if e[0] == "order"] == []
    assert any("does not allow flattening" in r.getMessage() and "skewed" in r.getMessage() for r in caplog.records)


def test_no_inventory_means_no_waiting_and_no_cancel(clock):
    info = Book(inv="0")
    mm = mm_with(info, clock)
    assert mm.flatten_on_start() == 0 and mm.exchange.events == [] and clock.t == 1000.0 and info.polls == 0


def test_dust_below_one_size_step_is_ignored(clock):
    mm = mm_with(Book(inv="0.00004"), clock)  # szDecimals 4: 0.0001 is the smallest tradable step
    assert mm.flatten_on_start() == 0 and mm.exchange.events == []


def test_a_stop_request_during_the_wait_ends_it_without_trading(clock):
    info = Book(inv="-0.02")
    mm = mm_with(info, clock, wait=60)
    mm._sleep = lambda s: (clock.sleep(s), mm.stop())
    mm.flatten_on_start()
    assert [e for e in mm.exchange.events if e[0] == "order"] == []


def test_partial_liquidity_is_used_and_the_rest_waits(clock):
    info = Book(inv="-0.02", asks=[("4160.0", "0.005")])  # less than the inventory, but something
    mm = mm_with(info, clock, wait=10)
    mm.exchange.fill = False  # the IOC only partly fills: inventory unchanged in this fake
    mm.flatten_on_start()
    assert [e for e in mm.exchange.events if e[0] == "order"]  # it still tried: a reduce-only IOC takes whatever is there


def test_the_grace_period_is_configurable_and_defaults_to_60s():
    assert MMConfig("XAU-HL").flatten_wait_s == 60 and MMConfig("XAU-HL", flatten_wait_s=5).flatten_wait_s == 5


def test_the_cli_flag_reaches_the_config(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from cli import sereel_cli

    seen = {}

    class FakeMM:
        def __init__(self, market, cfg):
            seen["cfg"] = cfg

        def run(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(mmod, "MarketMaker", FakeMM)
    monkeypatch.setattr(sereel_cli, "ROOT", tmp_path)
    monkeypatch.setattr(sereel_cli.signal, "signal", lambda *a: None)
    r = CliRunner().invoke(sereel_cli.app, ["mm", "run", "--market", "XAU-HL", "--size", "0.03", "--spread-bps", "10", "--levels", "3",
                                            "--center", "oracle", "--flatten-wait", "7", "--no-save"])
    assert r.exit_code == 0 and seen["cfg"].flatten_wait_s == 7
