"""The AI data layer: snapshot, Jev client, rules fallback, read-only guarantees."""
import ast
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from sqlmodel import Session, select

from app import pyth
from app.ai import extras, hl_readonly, jev, rules_signals, samples, signals
from app.ai.questions import MONITORING, NEEDS_STRATEGY, QUESTION_SET_VERSION, SETUP, question_names
from app.ai.state import build_snapshot, fmt, realized_vol
from app.config import ROOT, load_markets, settings
from app.db import engine
from app.models import PriceSample

D = Decimal
M = load_markets()["XAU-HL"]
NOW = datetime(2026, 10, 8, 9, 0, 0, tzinfo=timezone.utc)


def candle_series(n=170, start=4100.0, step=0.7):
    return [{"c": str(round(start + (i % 7) * step - (i % 5) * step * 0.6, 2))} for i in range(n)]


PAXG_LISTED = {"on": True}
OTHER_MARKS = {"flx:GOLD": "4150.0", "hyna:GOLD": "4993.2"}  # flx is within 1.5% of Pyth 4126.3; hyna is a 21% outlier


def fake_info(mainnet_fails=False, empty_book=False):
    def info(body, base=None):
        testnet = base == settings.hl_api_url
        if mainnet_fails and not testnet:
            raise hl_readonly.SignalsReadError(f"{body['type']}: HTTP 500")
        t = body["type"]
        if t == "metaAndAssetCtxs":
            dex = body.get("dex")
            if dex and f"{dex}:GOLD" in OTHER_MARKS:
                return [{"universe": [{"name": f"{dex}:GOLD"}]},
                        [{"markPx": OTHER_MARKS[f"{dex}:GOLD"], "oraclePx": "4126.3", "funding": "0.00000100", "openInterest": "1"}]]
            mark = "4135.4" if testnet else "4126.5"
            return [{"universe": [{"name": "xyz:GOLD"}]},
                    [{"markPx": mark, "oraclePx": "4126.3", "funding": "0.00000625", "openInterest": "73620.1"}]]
        if t == "candleSnapshot":
            return candle_series()
        if t == "l2Book":
            if empty_book:
                return {"levels": [[], []]}
            return {"levels": [[{"px": "4126.0", "sz": "10"}, {"px": "4100.0", "sz": "50"}],
                               [{"px": "4127.0", "sz": "20"}, {"px": "4150.0", "sz": "5"}]]}
        if t == "meta":
            return {"universe": [{"name": "BTC"}, {"name": "PAXG"}]} if PAXG_LISTED["on"] else {"universe": [{"name": "BTC"}]}
        if t == "predictedFundings":
            return [["BTC", []], ["PAXG", [["BinPerp", {"fundingRate": "0.00004", "fundingIntervalHours": 4}],
                                           ["HlPerp", {"fundingRate": "0.0000125", "fundingIntervalHours": 1}]]]]
        if t == "perpDexs":
            return [None, {"name": "xyz"}, {"name": "flx"}, {"name": "hyna"}]
        raise AssertionError(t)
    return info


@pytest.fixture
def net(monkeypatch):
    monkeypatch.setattr(hl_readonly, "info", fake_info())
    monkeypatch.setattr(extras, "pyth_history", lambda feed, now_s=None: ({"1h": D(2640), "24h": D(2600), "7d": D(2500)}, None))
    monkeypatch.setattr(pyth, "get_price", lambda *a, **k: pyth.PythPrice(D("4126.3"), 1791448800, "f", conf=D("0.16")))
    return monkeypatch


STRATEGY = {"id": "s1", "status": "active", "leverage": 3, "target_exposure_units": 0.2, "hedge_ratio_bps": 6000,
            "target_hedge_size_units": 0.12, "hedge_gap_units": 0.0, "hedge_gap_bps": 0, "rebalance_band_bps": 500,
            "required_margin_usd": 165.0, "value_usd": 130.0,
            "position": {"size_units": 0.12, "margin_usd": 127.2, "maintenance_margin_usd": 12.0, "unrealized_pnl_usd": -1.5,
                         "funding_paid_usd": 0.0, "mark_price_usd": 4126.5, "liquidation_price_usd": 5400.0}}


# ---- snapshot --------------------------------------------------------------------------------------------------------

def test_snapshot_text_is_deterministic_and_the_hash_follows_it(net):
    a, b = build_snapshot(M, STRATEGY, NOW), build_snapshot(M, STRATEGY, NOW)
    assert a.to_state_text() == b.to_state_text() and a.state_hash == b.state_hash and len(a.state_hash) == 64
    other = build_snapshot(M, {**STRATEGY, "value_usd": 131.0}, NOW)
    assert other.state_hash != a.state_hash
    assert build_snapshot(M, None, NOW).state_hash != a.state_hash


def test_snapshot_labels_networks_and_carries_the_core_fields(net):
    s = build_snapshot(M, STRATEGY, NOW)
    t = s.to_state_text()
    assert (s.signals_network, s.execution_network, s.degraded) == ("mainnet", "testnet", False)
    for line in ("signals_network: mainnet", "execution_network: testnet", "pyth_price: 4126.30", "pyth_confidence: 0.1600",
                 "hl_mark: 4126.50", "funding_rate_annualized: 5.48%", "spread_bps:", "depth_buy_within_0.5pct_oz:",
                 "change_24h_pct:", "vol_ratio_24h_7d:", "move_1h_sigmas:", "hourly_sigma_pct:", "mark_vs_pyth_bps: 0.5",
                 "testnet_depth_sell_within_0.5pct_oz:", "maintenance_ratio: 10.833", "equity_to_required_ratio: 0.79",
                 "distance_to_liquidation_pct: 30.86", "[calendar]"):
        assert line in t, line
    assert re.search(r"realized_vol_24h_annualized: \d+\.\d\d%", t) and re.search(r"realized_vol_7d_annualized: \d+\.\d\d%", t)


def test_the_testnet_figures_are_kept_but_labelled_as_not_a_market_signal(net):
    t = build_snapshot(M, None, NOW).to_state_text()
    assert "[execution venue (not a market signal)]" in t and "\n[execution]" not in t
    assert "testnet_mark: 4135.40" in t and "testnet_mark_vs_pyth_bps:" in t  # still in the record


def test_a_market_only_snapshot_has_no_strategy_section(net):
    s = build_snapshot(M, None, NOW)
    assert "[strategy]" not in s.to_state_text() and not s.has_strategy


def test_mainnet_failure_degrades_to_the_labelled_testnet_and_says_why(net):
    net.setattr(hl_readonly, "info", fake_info(mainnet_fails=True))
    s = build_snapshot(M, None, NOW)
    t = s.to_state_text()
    assert s.degraded and s.signals_network == "testnet" and "degraded: true" in t and "signals_network: testnet" in t
    assert "[unavailable]" in t and "signals_reads:" in t and "hl_mark: 4135.40" in t  # real testnet numbers, not mainnet's


def test_total_hl_failure_still_returns_a_snapshot_with_reasons(net):
    def dead(body, base=None):
        raise hl_readonly.SignalsReadError("down")
    net.setattr(hl_readonly, "info", dead)
    s = build_snapshot(M, None, NOW)
    assert s.degraded and {"signals_reads", "testnet_fallback", "volatility", "execution_book"} <= set(s.absent)
    assert "pyth_price: 4126.30" in s.to_state_text()


def test_each_optional_signal_can_fail_without_breaking_the_snapshot(net):
    net.setattr(extras, "pyth_history", lambda *a, **k: (None, "pyth history HTTP 500"))
    net.setattr(extras, "calendar", lambda *a, **k: (None, "calendar unavailable: OSError"))
    net.setattr(extras, "cross_venue", lambda *a, **k: (None, "cross-venue reads unavailable: HTTPError"))
    s = build_snapshot(M, STRATEGY, NOW)
    assert {"pyth_history", "calendar", "cross_venue"} <= set(s.absent) and not s.degraded
    assert "hl_mark: 4126.50" in s.to_state_text() and "pyth_history: pyth history HTTP 500" in s.to_state_text()


def test_empty_book_is_reported_not_guessed(net):
    net.setattr(hl_readonly, "info", fake_info(empty_book=True))
    assert build_snapshot(M, None, NOW).absent["book"] == "order book empty or unreadable"


def test_an_outlier_gold_market_is_excluded_from_the_text_and_listed_with_its_reason(net):
    s = build_snapshot(M, None, NOW)
    t = s.to_state_text()
    assert "flx:GOLD_mark: 4150.00" in t  # 0.6% from Pyth: context for Jev
    assert "hyna" not in t and "4993" not in t and "outside 1.5%" not in t  # never shown to Jev, mark included
    assert s.excluded_markets == [{"market": "hyna:GOLD", "mark": D("4993.2"), "reason": "outside 1.5% of Pyth"}]


def test_the_1_5_percent_boundary(net):
    OTHER_MARKS["flx:GOLD"] = str((D("4126.3") * D("1.015")).quantize(D("0.01")))  # 4188.19: just inside
    assert "flx:GOLD_mark" in build_snapshot(M, None, NOW).to_state_text()
    OTHER_MARKS["flx:GOLD"] = str((D("4126.3") * D("1.0151")).quantize(D("0.01")))  # just outside
    s = build_snapshot(M, None, NOW)
    assert "flx" not in s.to_state_text() and {e["market"] for e in s.excluded_markets} == {"flx:GOLD", "hyna:GOLD"}
    OTHER_MARKS["flx:GOLD"] = "4150.0"


def test_venue_divergence_reads_the_mainnet_mark_vs_pyth_only(net):
    OTHER_MARKS["flx:GOLD"] = "4400.0"  # a wild other market: excluded, and never the trigger anyway
    try:
        s = build_snapshot(M, None, NOW)
        assert rules_signals.answer(s, ["venue_price_divergence"])["venue_price_divergence"] == D("0.05")  # 0.5 bps
        s.sections["cross_venue"] = {"flx:GOLD_mark": "9999.00"}  # absurd context changes nothing
        s.sections["execution"]["testnet_mark_vs_pyth_bps"] = "250.0"  # the execution venue is not a market signal: ignored
        assert rules_signals.answer(s, ["venue_price_divergence"])["venue_price_divergence"] == D("0.05")
        s.sections["venue"]["mark_vs_pyth_bps"] = "-16.0"  # the mainnet mark itself diverging is what triggers (absolute value)
        assert rules_signals.answer(s, ["venue_price_divergence"])["venue_price_divergence"] >= D("0.9")
    finally:
        OTHER_MARKS["flx:GOLD"] = "4150.0"


def test_paxg_funding_is_a_per_hour_reference_when_listed_and_skipped_when_not(net):
    t = build_snapshot(M, None, NOW).to_state_text()
    assert "paxg_perp_funding_hourly_BinPerp: 0.00001000" in t  # 4h rate 0.00004 -> per hour
    assert "paxg_perp_funding_hourly_HlPerp: 0.00001250" in t
    PAXG_LISTED["on"] = False
    try:
        t = build_snapshot(M, None, NOW).to_state_text()
        assert "paxg_perp" not in t
    finally:
        PAXG_LISTED["on"] = True


def test_absent_signals_carry_the_agreed_reasons(net):
    t = build_snapshot(M, None, NOW).to_state_text()
    assert "predicted_funding_xyz: HIP-3 market not covered by predictedFundings" in t
    assert "paxg_jupiter: mint not verified" in t


def test_extras_failures_return_reasons_never_raise(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    v, why = extras.pyth_history("ab" * 32, 1_790_000_000)
    assert v is None and "unavailable" in why
    monkeypatch.setattr(hl_readonly, "info", lambda *a, **k: (_ for _ in ()).throw(hl_readonly.SignalsReadError("x")))
    v, why = extras.cross_venue("xyz:GOLD", D(4126))
    assert v is None and "unavailable" in why


def test_realized_vol_and_formatting_are_stable():
    assert realized_vol([D(1), D(2)]) is None
    closes = [D(str(100 + (i % 3))) for i in range(30)]
    assert realized_vol(closes) == realized_vol(list(closes)) and realized_vol(closes) > 0
    assert fmt(None) == "n/a" and fmt("1.23456", 2) == "1.23" and fmt(D("0.0000125"), 8) == "0.00001250"


# ---- calendar -------------------------------------------------------------------------------------------------------

def test_every_event_is_sourced_from_the_agency_and_dated():
    events = yaml.safe_load((ROOT / "data" / "events.yaml").read_text())
    assert events
    for e in events:
        assert e["source_url"].startswith(("https://www.federalreserve.gov/", "https://www.bls.gov/")), e
        assert e["verified_on"] and e["kind"] in ("fomc", "cpi") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(e["date"])), e
        datetime.strptime(str(e["date"]), "%Y-%m-%d")


def test_calendar_window_and_next_event():
    ev = [{"kind": "cpi", "name": "CPI", "date": "2026-10-14", "time": "08:30", "tz": "America/New_York"},
          {"kind": "fomc", "name": "FOMC", "date": "2026-10-28", "time": None, "tz": "America/New_York"}]
    c, _ = extras.calendar(datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc), ev)  # 09:00 ET the day before: 23.5h away
    assert c["within_24h"] == ["CPI"]
    c, _ = extras.calendar(datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc), ev)  # 24.5h away
    assert c["within_24h"] == [] and c["next_event"] == "CPI on 2026-10-14"
    c, _ = extras.calendar(datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc), ev)  # FOMC day, in progress
    assert c["within_24h"] == ["FOMC"]
    c, _ = extras.calendar(datetime(2026, 11, 2, tzinfo=timezone.utc), ev)
    assert c["within_24h"] == [] and c["next_event"] == "none listed"


# ---- read-only guarantee ---------------------------------------------------------------------------------------------

def test_signals_code_can_only_read_never_sign_or_trade():
    src = (ROOT / "app" / "ai" / "hl_readonly.py").read_text()
    tree = ast.parse(src)
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
               {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(x.startswith(("hyperliquid", "eth_account")) for x in imported)
    code = src.replace(ast.get_docstring(tree, clean=False), "")  # the docstring explains the rule by naming what is forbidden
    assert "/exchange" not in code and "Exchange" not in code and "private" not in code.lower()
    assert "/info" in code


def test_mainnet_signals_url_is_never_used_for_orders_and_the_execution_guard_still_holds():
    assert settings.signals_source_network == "mainnet" and "testnet" not in settings.signals_hl_url
    assert settings.is_hl_testnet  # HL_API_URL (execution) stays testnet
    settings.assert_network_safe()


def test_every_http_call_in_the_ai_package_has_a_timeout():
    seen = 0
    for path in (ROOT / "app" / "ai").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "httpx" and node.func.attr in ("get", "post", "Client"):
                seen += 1
                assert any(k.arg == "timeout" for k in node.keywords), f"{path.name}:{node.lineno}"
    assert seen >= 3  # hl_readonly.post, jev's Client, extras' pyth history


def test_signals_source_network_testnet_reads_the_execution_url(monkeypatch):
    monkeypatch.setattr(settings, "signals_source_network", "testnet")
    assert settings.signals_url == settings.hl_api_url


# ---- Jev client ------------------------------------------------------------------------------------------------------

def jev_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


OK_BODY = {"model": "jev-1.13.0", "answers": {"abnormal_price_move": {"type": "noul", "noul": 0.12},
                                             "venue_price_divergence": {"type": "noul", "noul": 0.9}},
           "usage": {"input_tokens": 300, "output_tokens": 16}}
NAMES = ["abnormal_price_move", "venue_price_divergence"]


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(settings, "jev_api_key", "ng-secret-key-123")
    monkeypatch.setattr(settings, "jev_auth_header", "auto")


def test_jev_request_shape_and_response_parsing(key):
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers.get("authorization"), __import__("json").loads(req.content)
        return httpx.Response(200, json=OK_BODY)

    r = jev.ask("state text", NAMES, jev_client(handler))
    assert seen["url"] == "https://gateway.ngrok.ai/v1/systemone" and seen["auth"] == "Bearer ng-secret-key-123"
    assert seen["body"]["model"] == "jev-latest" and seen["body"]["state"] == "state text"
    q = seen["body"]["questions"]["abnormal_price_move"]
    assert q["type"] == "noul" and q["instructions"] and set(q["criteria"]) == {"true", "false"}
    assert r.probabilities == {"abnormal_price_move": D("0.12"), "venue_price_divergence": D("0.9")}
    assert r.auth_header == "Authorization: Bearer" and r.model == "jev-1.13.0" and r.usage["input_tokens"] == 300 and r.raw == OK_BODY


def test_a_401_on_bearer_falls_through_to_x_api_key_and_reports_which_worked(key):
    calls = []

    def handler(req):
        calls.append(dict(req.headers))
        return httpx.Response(401) if "authorization" in req.headers else httpx.Response(200, json=OK_BODY)

    r = jev.ask("s", NAMES, jev_client(handler))
    assert r.auth_header == "x-api-key" and calls[-1]["x-api-key"] == "ng-secret-key-123" and len(calls) == 2


def test_pinned_header_setting_is_respected(key, monkeypatch):
    monkeypatch.setattr(settings, "jev_auth_header", "x-api-key")
    seen = []
    jev.ask("s", NAMES, jev_client(lambda req: seen.append(dict(req.headers)) or httpx.Response(200, json=OK_BODY)))
    assert "authorization" not in seen[0] and seen[0]["x-api-key"]
    monkeypatch.setattr(settings, "jev_auth_header", "nonsense")
    with pytest.raises(jev.JevError):
        jev.ask("s", NAMES, jev_client(lambda req: httpx.Response(200, json=OK_BODY)))


def test_both_headers_rejected_is_a_clear_error(key):
    with pytest.raises(jev.JevError) as e:
        jev.ask("s", NAMES, jev_client(lambda req: httpx.Response(401)))
    assert "401" in str(e.value) and "ng-secret-key-123" not in str(e.value)


def test_one_retry_on_5xx_and_on_timeout_then_success(key):
    n = {"c": 0}

    def flaky(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=OK_BODY)

    assert jev.ask("s", NAMES, jev_client(flaky)).probabilities and n["c"] == 2
    n["c"] = 0

    def timeouts(req):
        n["c"] += 1
        if n["c"] == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json=OK_BODY)

    assert jev.ask("s", NAMES, jev_client(timeouts)).probabilities and n["c"] == 2
    n["c"] = 0
    with pytest.raises(jev.JevError):  # persistent failure: one retry, then give up
        jev.ask("s", NAMES, jev_client(lambda req: n.__setitem__("c", n["c"] + 1) or httpx.Response(503)))
    assert n["c"] == 2  # the first try and exactly one retry


@pytest.mark.parametrize("body", [{"answers": {}}, {"answers": {"abnormal_price_move": {"type": "noul"}}},
                                  {"answers": {"abnormal_price_move": {"noul": 1.4}, "venue_price_divergence": {"noul": 0.2}}}, {}])
def test_malformed_or_out_of_range_answers_are_errors(key, body):
    with pytest.raises(jev.JevError):
        jev.ask("s", NAMES, jev_client(lambda req: httpx.Response(200, json=body)))


def test_the_key_is_never_logged(key, caplog):
    with caplog.at_level(logging.DEBUG):
        jev.ask("s", NAMES, jev_client(lambda req: httpx.Response(200, json=OK_BODY)))
        with pytest.raises(jev.JevError):
            jev.ask("s", NAMES, jev_client(lambda req: httpx.Response(401)))
    assert "ng-secret-key-123" not in caplog.text
    assert "model=jev-1.13.0" in caplog.text and "latency=" in caplog.text and "in=300" in caplog.text


def test_no_key_means_jev_is_skipped_with_a_reason(monkeypatch):
    monkeypatch.setattr(settings, "jev_api_key", "")
    with pytest.raises(jev.JevError) as e:
        jev.ask("s", NAMES)
    assert "JEV_API_KEY is not set" in str(e.value)


# ---- Jev vs rules ---------------------------------------------------------------------------------------------------

def test_jev_answers_are_used_when_available(net, key):
    snap = build_snapshot(M, None, NOW)
    names = ["abnormal_price_move", "venue_price_divergence"]
    net.setattr(jev, "ask", lambda state, n, client=None: jev.JevResult({x: D("0.3") for x in n}, "jev-1.13.0", 5, "x-api-key"))
    s = signals.get_signals(snap, names)
    assert s.source == "jev" and s.probabilities == {x: D("0.3") for x in names} and s.jev_result.auth_header == "x-api-key"


def test_jev_failure_falls_back_to_rules_and_says_so(net, key):
    snap = build_snapshot(M, None, NOW, size_oz=D("0.05"))
    net.setattr(jev, "ask", lambda *a, **k: (_ for _ in ()).throw(jev.JevError("down")))
    s = signals.get_signals(snap)
    assert s.source == "rules" and s.jev_error == "down" and s.question_set_version == QUESTION_SET_VERSION
    assert set(s.probabilities) == set(question_names(snap)) and all(0 <= p <= 1 for p in s.probabilities.values())


def test_question_registry_is_consistent():
    assert NEEDS_STRATEGY <= set(MONITORING) and "high_impact_event_soon" in MONITORING and QUESTION_SET_VERSION == "2026-10-08.2"
    assert set(SETUP) == {"volatility_elevated", "funding_favors_shorts", "liquidity_sufficient_for_size"}
    for q in {**MONITORING, **SETUP}.values():
        assert q["type"] == "noul" and q["instructions"].endswith(".") and set(q["criteria"]) == {"true", "false"}
        assert all(q["criteria"].values()) and re.search(r"\d", q["instructions"])  # a number to compare against, in every question


FIELDS_READ = {  # question -> (state field names it names, a threshold phrase it must state)
    "volatility_elevated": (["vol_ratio_24h_7d", "realized_vol_24h_annualized"], ["1.3", "30%"]),
    "venue_price_divergence": (["mark_vs_pyth_bps"], ["15", "[execution venue (not a market signal)]"]),
    "funding_favors_shorts": (["funding_rate_annualized"], ["+2%", "longs pay shorts"]),
    "liquidity_sufficient_for_size": (["size_oz", "depth_to_size_ratio"], ["TESTNET", "2"]),
    "liquidity_sufficient": (["size_oz", "depth_to_size_ratio"], ["TESTNET", "2"]),
    "needs_top_up_soon": (["maintenance_ratio", "move_1h_sigmas"], ["1.5", "1.8"]),
    "should_rebalance": (["gap_pct", "rebalance_band_pct", "size_notional_usd"], ["10.50"]),
    "abnormal_price_move": (["move_1h_sigmas"], ["3"]),
    "excess_margin_safe_to_return": (["maintenance_ratio", "equity_to_required_ratio"], ["3.0", "2.0"]),
    "high_impact_event_soon": (["events_within_24h"], ["none"]),
}


def test_every_question_names_real_state_fields_and_states_its_threshold(net):
    text = build_snapshot(M, {**STRATEGY, "hedge_gap_units": 0.02, "hedge_gap_bps": 1600}, NOW).to_state_text()
    allq = {**MONITORING, **SETUP}
    assert set(FIELDS_READ) == set(allq)
    for name, (fields, phrases) in FIELDS_READ.items():
        q = allq[name]
        for f in fields:
            assert f in q["instructions"] and f in q["criteria"]["true"] + q["criteria"]["false"] + q["instructions"], (name, f)
            if f not in ("size_oz",) or "[sizing]" in text:
                assert f"{f}:" in text, f"{name}: state has no field {f}"
        for ph in phrases:
            assert ph in q["instructions"] + q["criteria"]["true"] + q["criteria"]["false"], (name, ph)


def test_rules_cover_every_question_with_sensible_directions(net):
    gap = {**STRATEGY, "hedge_gap_units": 0.02, "hedge_gap_bps": 1600}  # a 16% gap: a real rebalance
    snap = build_snapshot(M, gap, NOW)
    healthy = rules_signals.answer(snap, question_names(snap))
    assert set(healthy) == set(MONITORING) and healthy["needs_top_up_soon"] < D("0.5") and healthy["excess_margin_safe_to_return"] < D("0.5")
    assert healthy["should_rebalance"] >= D("0.9")  # gap 16% > 5% band and $82 of notional
    weak = build_snapshot(M, {**gap, "value_usd": 14.0}, NOW)  # ratio 1.17
    weak.sections["sizing"]["depth_to_size_ratio"] = "0.50"
    a = rules_signals.answer(weak, question_names(weak))
    assert a["needs_top_up_soon"] >= D("0.9") and a["liquidity_sufficient"] <= D("0.1")
    rich = build_snapshot(M, {**gap, "value_usd": 500.0, "required_margin_usd": 100.0}, NOW)  # ratio 41.7, equity 5x required
    assert rules_signals.answer(rich, ["excess_margin_safe_to_return"])["excess_margin_safe_to_return"] >= D("0.9")
    with pytest.raises(KeyError):
        rules_signals.answer(build_snapshot(M, None, NOW), ["not_a_question"])


@pytest.mark.parametrize("field,section,value,question,expected", [
    ("vol_ratio_24h_7d", "volatility", "1.31", "volatility_elevated", "yes"), ("vol_ratio_24h_7d", "volatility", "1.30", "volatility_elevated", "no"),
    ("realized_vol_24h_annualized", "volatility", "30.01%", "volatility_elevated", "yes"),
    ("funding_rate_annualized", "venue", "2.01%", "funding_favors_shorts", "yes"), ("funding_rate_annualized", "venue", "0.99%", "funding_favors_shorts", "no"),
    ("funding_rate_annualized", "venue", "1.50%", "funding_favors_shorts", "maybe"), ("funding_rate_annualized", "venue", "-3.00%", "funding_favors_shorts", "no"),
    ("mark_vs_pyth_bps", "venue", "15.1", "venue_price_divergence", "yes"), ("mark_vs_pyth_bps", "venue", "-15.1", "venue_price_divergence", "yes"),
    ("mark_vs_pyth_bps", "venue", "7.9", "venue_price_divergence", "no"), ("mark_vs_pyth_bps", "venue", "10.0", "venue_price_divergence", "maybe"),
    ("move_1h_sigmas", "volatility", "3.01", "abnormal_price_move", "yes"), ("move_1h_sigmas", "volatility", "1.99", "abnormal_price_move", "no"),
    ("move_1h_sigmas", "volatility", "2.50", "abnormal_price_move", "maybe"),
])
def test_rules_apply_exactly_the_thresholds_the_questions_state(net, field, section, value, question, expected):
    snap = build_snapshot(M, None, NOW)
    snap.sections[section][field] = value
    p = rules_signals.answer(snap, [question])[question]
    assert {"yes": p >= D("0.9"), "no": p <= D("0.1"), "maybe": D("0.4") <= p <= D("0.6")}[expected], (field, value, p)


def test_a_liquidity_question_is_only_asked_when_a_size_is_known_and_the_absence_is_reported(net):
    none = build_snapshot(M, None, NOW)
    assert "liquidity_sufficient_for_size" not in question_names(none) and "sizing" not in none.sections
    assert none.absent["liquidity_sufficient_for_size"] == "no hedge size given"
    assert "liquidity_sufficient_for_size: no hedge size given" in none.to_state_text()
    sized = build_snapshot(M, None, NOW, size_oz=D("0.005"))
    assert "liquidity_sufficient_for_size" in question_names(sized)
    sec = sized.sections["sizing"]
    assert sec["size_oz"] == "0.0050" and sec["side_to_trade"] == "sell" and sec["size_basis"].startswith("proposed hedge")
    assert sec["testnet_depth_same_side_within_0.5pct_oz"] == "10.0000"  # the fake testnet book: bids within 0.5% of its mark are the 10 oz at 4126
    assert sec["depth_to_size_ratio"] == "2000.00"
    flat = build_snapshot(M, {**STRATEGY, "hedge_gap_units": 0.0}, NOW)
    assert "liquidity_sufficient" not in question_names(flat) and "no rebalance needed" in flat.absent["liquidity_sufficient"]


def test_the_rebalance_side_follows_the_sign_of_the_gap_and_uses_that_sides_depth(net):
    under = build_snapshot(M, {**STRATEGY, "hedge_gap_units": 0.02}, NOW).sections["sizing"]  # short more: a sell, hits the bids
    over = build_snapshot(M, {**STRATEGY, "hedge_gap_units": -0.02}, NOW).sections["sizing"]  # buy back: lifts the asks
    assert (under["side_to_trade"], over["side_to_trade"]) == ("sell", "buy")
    assert under["testnet_depth_same_side_within_0.5pct_oz"] != over["testnet_depth_same_side_within_0.5pct_oz"]
    assert under["size_oz"] == over["size_oz"] == "0.0200" and under["size_notional_usd"] == "82.53"


def test_rules_detect_events(net):
    snap = build_snapshot(M, None, NOW)
    assert rules_signals.answer(snap, ["high_impact_event_soon"])["high_impact_event_soon"] <= D("0.1")
    snap.sections["calendar"]["events_within_24h"] = "CPI release"
    assert rules_signals.answer(snap, ["high_impact_event_soon"])["high_impact_event_soon"] >= D("0.9")


# ---- price samples ---------------------------------------------------------------------------------------------------

def test_sampler_writes_both_sources_and_prunes_old_rows(net):
    net.setattr(pyth, "get_price", lambda *a, **k: pyth.PythPrice(D("4126.3"), 1, "f"))
    net.setattr(hl_readonly, "info", fake_info())
    assert samples.sample_once() == 2
    with Session(engine) as s:
        rows = s.exec(select(PriceSample)).all()
        assert {(r.market_id, r.source) for r in rows} == {("XAU-HL", "pyth"), ("XAU-HL", "hl_signals")}
        assert {r.price for r in rows} == {D("4126.3"), D("4126.5")}
        old = PriceSample(market_id="XAU-HL", source="pyth", price=D(1), ts=datetime(2020, 1, 1, tzinfo=timezone.utc))
        s.add(old)
        s.commit()
    samples.sample_once()
    with Session(engine) as s:
        assert all(r.ts.year >= 2026 for r in s.exec(select(PriceSample)).all())


def test_a_failing_source_does_not_stop_the_other(net):
    net.setattr(pyth, "get_price", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert samples.sample_once() == 1
