"""Hyperliquid connection behaviour.

The SDK defaults to NO request timeout (a silent peer blocks a thread forever: it wedged a server's shutdown and a market maker's
for 25+ minutes). A flat short timeout then broke STARTUP, because every SDK client downloads large metadata, three clients meant
a dozen downloads, and Hyperliquid can take 7-11 s per call. So: one shared Info downloads (long connect timeout, retried), every
Exchange is built offline and pointed at it, and afterwards every client drops to the short per-request timeout.
"""
import re
from pathlib import Path

import pytest
import requests

from app.config import load_markets, settings
from app.errors import ServiceError
from app.mm import market_maker as mmod
from app.venue import hyperliquid as hlmod

KEY = "0x" + "11" * 32


class FakeInfo:
    """Stands in for the SDK Info: records how it was built; has the timeout attribute the SDK's API base class has."""
    made = []
    fail_first = 0
    fail_with = requests.exceptions.ReadTimeout

    def __init__(self, base_url=None, skip_ws=False, meta=None, spot_meta=None, perp_dexs=None, timeout=None):
        type(self).made.append(self)
        if len(type(self).made) <= type(self).fail_first:
            raise type(self).fail_with("Read timed out.")
        self.kw = {"perp_dexs": perp_dexs, "timeout": timeout}
        self.timeout = timeout

    def meta(self, dex=""):
        return {"universe": [{"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25}]}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    FakeInfo.made, FakeInfo.fail_first, FakeInfo.fail_with = [], 0, requests.exceptions.ReadTimeout
    monkeypatch.setattr(hlmod, "Info", FakeInfo)
    monkeypatch.setattr(hlmod.time, "sleep", lambda s: None)  # no real pauses between retries
    for k, v in (("hl_account_address", "0xme"), ("hl_api_wallet_key", KEY), ("hl_master_key", "0x" + "22" * 32),
                 ("hl_mm_account_address", "0xmm"), ("hl_mm_api_wallet_key", "0x" + "33" * 32),
                 ("hl_connect_timeout_s", 60.0), ("hl_request_timeout_s", 7.0), ("hl_connect_attempts", 4)):
        monkeypatch.setattr(settings, k, v)

    def no_network(*a, **k):
        raise AssertionError("an Exchange must be built without touching the network")

    monkeypatch.setattr(requests.Session, "post", no_network)  # the real SDK Exchange is used below; any download would trip this


def venue(monkeypatch):
    monkeypatch.setattr(hlmod.HyperliquidVenue, "resolve_markets", lambda self: None)
    monkeypatch.setattr(hlmod, "account_mode", lambda info, addr: "default")
    return hlmod.HyperliquidVenue(load_markets())


def test_defaults_are_sane():
    from app.config import Settings

    s = Settings(_env_file=None)
    assert 0 < s.hl_request_timeout_s <= 60 and s.hl_connect_timeout_s > s.hl_request_timeout_s and s.hl_connect_attempts >= 2


def test_an_exchange_is_built_offline_and_uses_the_shared_info(monkeypatch):
    info = FakeInfo(timeout=60)
    ex = hlmod.exchange_for(KEY, "0xme", info)  # the REAL SDK Exchange; Session.post would raise on any download
    assert ex.info is info and ex.account_address == "0xme"
    assert ex.timeout == 60 and ex.wallet.address  # the connect timeout until use_request_timeouts is called


def test_the_venue_downloads_metadata_once_and_shares_it(monkeypatch):
    v = venue(monkeypatch)
    assert len(FakeInfo.made) == 1  # one Info, not one per client
    assert FakeInfo.made[0].kw["perp_dexs"] == ["", "xyz"]
    assert v.exchange.info is v.info and v._master_exchange.info is v.info


def test_connect_timeout_while_connecting_then_the_short_request_timeout_everywhere(monkeypatch):
    seen = {}
    monkeypatch.setattr(hlmod.HyperliquidVenue, "resolve_markets", lambda self: seen.update(info=self.info.timeout, ex=self.exchange.timeout))
    monkeypatch.setattr(hlmod, "account_mode", lambda info, addr: "default")
    v = hlmod.HyperliquidVenue(load_markets())
    assert seen == {"info": 60.0, "ex": 60.0}  # long while connecting
    assert v.info.timeout == v.exchange.timeout == v._master_exchange.timeout == 7.0  # short once connected


def test_transient_connect_failures_are_retried_until_it_works(monkeypatch):
    FakeInfo.fail_first = 2
    venue(monkeypatch)
    assert len(FakeInfo.made) == 3  # two timeouts, then success


@pytest.mark.parametrize("err", [requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError, ConnectionError, TimeoutError])
def test_all_the_usual_network_errors_count_as_transient(monkeypatch, err):
    FakeInfo.fail_first, FakeInfo.fail_with = 1, err
    venue(monkeypatch)
    assert len(FakeInfo.made) == 2


def test_giving_up_is_a_clear_venue_unavailable_not_a_traceback_wall(monkeypatch):
    FakeInfo.fail_first = 99
    with pytest.raises(ServiceError) as e:
        venue(monkeypatch)
    assert (e.value.code, e.value.status) == ("VENUE_UNAVAILABLE", 503)
    assert "4 attempts" in e.value.message and "HL_CONNECT_TIMEOUT_S" in e.value.message and "loading market metadata" in e.value.message
    assert len(FakeInfo.made) == 4


def test_a_non_network_error_is_never_retried(monkeypatch):
    FakeInfo.fail_first, FakeInfo.fail_with = 99, ValueError
    with pytest.raises(ValueError):
        venue(monkeypatch)
    assert len(FakeInfo.made) == 1


def test_later_connect_steps_are_retried_too(monkeypatch):
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(hlmod.HyperliquidVenue, "resolve_markets", flaky)
    monkeypatch.setattr(hlmod, "account_mode", lambda info, addr: "default")
    hlmod.HyperliquidVenue(load_markets())
    assert calls["n"] == 3 and len(FakeInfo.made) == 1  # the resolve step retried without re-downloading the metadata


def test_the_market_maker_shares_one_info_and_switches_timeouts(monkeypatch):
    monkeypatch.setattr(mmod, "account_mode", lambda info, addr: "unifiedAccount")
    mm = mmod.MarketMaker(load_markets()["XAU-HL"], mmod.MMConfig("XAU-HL"))
    assert len(FakeInfo.made) == 1 and mm.exchange.info is mm.info
    assert mm.info.timeout == mm.exchange.timeout == 7.0


def test_the_market_maker_retries_a_slow_connect(monkeypatch):
    FakeInfo.fail_first = 2
    monkeypatch.setattr(mmod, "account_mode", lambda info, addr: "unifiedAccount")
    mmod.MarketMaker(load_markets()["XAU-HL"], mmod.MMConfig("XAU-HL"))
    assert len(FakeInfo.made) == 3


def test_no_client_is_constructed_without_a_timeout_anywhere_in_the_app():
    """A grep-style guard: any new Info(...) / Exchange(...) in app/ must pass timeout=."""
    offenders = []
    for path in (Path(__file__).parent.parent / "app").rglob("*.py"):
        text = path.read_text()
        for m in re.finditer(r"(?<![\w.])(?:Info|Exchange)\(", text):
            call = text[m.start():m.start() + 500]
            depth, end, opened = 0, None, False
            for i, ch in enumerate(call):
                depth += ch == "("
                depth -= ch == ")"
                opened = opened or ch == "("
                if opened and depth == 0:
                    end = i
                    break
            assert end is not None, f"could not scan {path.name}"
            if "timeout=" not in call[:end]:
                offenders.append(f"{path.name}: {call[:60]!r}")
    assert not offenders, offenders
