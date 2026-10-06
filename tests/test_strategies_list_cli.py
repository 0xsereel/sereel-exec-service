import json

import httpx
import pytest
from typer.testing import CliRunner

from cli import strategies as cli_strategies
from cli.sereel_cli import app

runner = CliRunner(env={"COLUMNS": "220"})  # a realistic terminal width; the table has 12 columns


def strat(i, status="active", size=0.12, **kw):
    pos = {"side": "short", "size_units": size, "entry_price_usd": 4153.33, "mark_price_usd": 4160.5, "margin_usd": 202.0,
           "unrealized_pnl_usd": -0.86, "realized_pnl_usd": 0, "funding_paid_usd": 0, "fees_usd": 0.45, "margin_health_bps": 9500,
           "maintenance_margin_usd": 9.9, "liquidation_price_usd": 5447.9, "hl_order_ids": ["1"]} if status in ("active", "closing") else None
    base = {"id": f"{i:08d}-aaaa-bbbb-cccc-dddddddddddd", "fund_id": f"fund-{i}", "market_symbol": "XAU", "status": status,
            "target_hedge_size_units": 0.12, "position": pos, "owner_pubkey": "4DZJkNoRkAjFH3PY84PbHYhe8MjsYUoaC4t7ptr5fJxH",
            "owner_multisig": None, "market_closed": False, "expected_amount_usd": 202.0, "received_amount_usd": None,
            "shortfall_usd": None, "expires_at": "2026-10-05T12:00:00Z", "failure_reason": None}
    base.update(kw)
    return base


def serve(monkeypatch, rows, status=200, seen=None):
    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=rows if status == 200 else {"error": "x", "code": "UNAUTHORIZED"}, request=request)

    monkeypatch.setattr(cli_strategies, "_client", lambda url: httpx.Client(base_url=url, transport=httpx.MockTransport(handler),
                                                                           headers={"X-Sereel-Key": "k"}))


def test_default_lists_only_live_strategies_with_value_and_health(monkeypatch):
    serve(monkeypatch, [strat(1), strat(2, "closed"), strat(3, "failed"), strat(4, "pending_funding", received_amount_usd=100.0,
                                                                              shortfall_usd=102.0), strat(5, "cancelled")])
    r = runner.invoke(app, ["strategies", "list"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "strategies (2)" in out and "00000001" in out and "00000004" in out
    assert "00000002" not in out and "00000003" not in out and "00000005" not in out  # terminal ones hidden by default
    assert "0.12 short" in out and "201.14" in out and "-0.86" in out and "95.0%" in out  # value = margin + unrealized
    assert "4DZJ...fJxH" in out
    flat = " ".join(out.split())
    assert "pending_funding" in flat and "expected 202.0" in flat and "received 100.0" in flat and "shortfall 102.0" in flat


def test_all_flag_includes_terminal_strategies(monkeypatch):
    serve(monkeypatch, [strat(1), strat(2, "closed"), strat(3, "failed", failure_reason="NO_LIQUIDITY: nothing")])
    out = runner.invoke(app, ["strategies", "list", "--all"]).output
    assert "strategies (3)" in out and "00000002" in out and "00000003" in out


def test_json_prints_the_raw_response(monkeypatch):
    rows = [strat(1), strat(2, "closed")]
    serve(monkeypatch, rows)
    r = runner.invoke(app, ["strategies", "list", "--json"])
    assert json.loads(r.output) == [rows[0]]  # filtered like the table; --all --json gives everything
    assert json.loads(runner.invoke(app, ["strategies", "list", "--json", "--all"]).output) == rows


def test_filters_are_sent_to_the_api_and_the_key_is_attached(monkeypatch):
    seen = []
    serve(monkeypatch, [], seen=seen)
    runner.invoke(app, ["strategies", "list", "--owner", "u1", "--fund-id", "f9"])
    (req,) = seen
    assert req.url.path == "/strategies" and dict(req.url.params) == {"owner": "u1", "fund_id": "f9"}
    assert req.headers["x-sereel-key"] == "k"


@pytest.mark.parametrize("bps,expect", [(9500, "green"), (7500, "green"), (7499, "yellow"), (4000, "yellow"), (3999, "red"), (0, "red")])
def test_health_colours(bps, expect):
    assert expect in cli_strategies._health_cell(bps)


def test_helpful_errors(monkeypatch):
    serve(monkeypatch, {}, status=401)
    r = runner.invoke(app, ["strategies", "list"])
    assert r.exit_code == 1 and "API_KEY" in " ".join(r.output.split())

    def refused(url):
        def handler(request):
            raise httpx.ConnectError("refused", request=request)

        return httpx.Client(base_url=url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli_strategies, "_client", refused)
    r = runner.invoke(app, ["strategies", "list", "--url", "http://nowhere:1"])
    assert r.exit_code == 1 and "is `sereel serve` running" in " ".join(r.output.split()) and "http://nowhere:1" in r.output


def test_an_empty_list_is_not_an_error(monkeypatch):
    serve(monkeypatch, [])
    r = runner.invoke(app, ["strategies", "list"])
    assert r.exit_code == 0 and "strategies (0)" in r.output
