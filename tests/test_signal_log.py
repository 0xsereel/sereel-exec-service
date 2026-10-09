"""The agent cycle log: every Jev answer and decision, one line per strategy per cycle, in the server log and a rotating JSONL file."""
import json
import logging
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from app.ai import jev, loop, signal_log
from app.config import settings
from cli.sereel_cli import app as cli_app
from test_agent import cycle, run_once_req  # noqa: F401  (the fake-market fixture and a signed run-once helper)
from test_step1 import active

D = Decimal
runner = CliRunner()


def lines():
    path = signal_log._path()
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_every_cycle_writes_one_line_with_every_jev_answer_and_the_decision(api, fakechain, cycle, caplog):
    calls, probs = cycle
    probs.update(should_rebalance="0.31")
    sid = active(api, fakechain)["id"]
    with caplog.at_level(logging.INFO, logger="sereel.agent"):
        assert loop.run_cycle() == 1
        assert loop.run_cycle() == 1  # a second cycle is a second line: the history is kept even though the DB heartbeat is updated in place
    got = lines()
    assert len(got) == 2 and got[0]["strategy_id"] == sid
    e = got[0]
    assert e["source"] == "jev" and e["decision"] == "none" and e["action"] is None and e["reason"] == "no rule fired"
    assert e["probabilities"]["should_rebalance"] == "0.31" and e["probabilities"]["needs_top_up_soon"] == "0.05"
    assert set(e["probabilities"]) >= {"needs_top_up_soon", "should_rebalance", "abnormal_price_move", "venue_price_divergence", "liquidity_sufficient",
                                       "excess_margin_safe_to_return", "high_impact_event_soon"} - {"liquidity_sufficient"}
    assert e["jev"] == {"model": "jev-1.13.0", "latency_ms": 5, "usage": {}, "auth_header": "Authorization: Bearer", "response": {}}
    assert len(e["state_hash"]) == 64 and e["signals_network"] == "mainnet" and e["question_set_version"] and e["at"].endswith("Z")
    assert "state" not in e  # the state text is off unless asked for
    console = [r.getMessage() for r in caplog.records if "signals (" in r.getMessage()]
    assert len(console) == 2 and "should_rebalance=0.31" in console[0] and "-> none" in console[0] and sid[:8] in console[0] and "5ms" in console[0]


def test_a_proposal_is_logged_with_its_action_and_reason(api, fakechain, cycle, monkeypatch):
    from test_agent import weaken
    calls, probs = cycle
    sid = active(api, fakechain)["id"]
    weaken(monkeypatch)
    probs.update(needs_top_up_soon="0.93")
    loop.run_cycle()
    e = lines()[-1]
    assert e["decision"] == "propose" and e["action"]["type"] == "top_up" and e["action"]["params"]["amount_usd"].isdigit() and "needs_top_up_soon = 0.93" in e["reason"]


def test_when_jev_is_down_the_line_says_rules_and_why(api, fakechain, cycle, monkeypatch):
    sid = active(api, fakechain)["id"]
    monkeypatch.setattr(jev, "ask", lambda *a, **k: (_ for _ in ()).throw(jev.JevError("HTTP 503")))
    loop.run_cycle()
    e = lines()[-1]
    assert e["source"] == "rules" and e["jev_error"] == "HTTP 503" and "jev" not in e and e["probabilities"]


def test_the_full_state_text_is_logged_only_when_asked(api, fakechain, cycle, monkeypatch):
    sid = active(api, fakechain)["id"]
    monkeypatch.setattr(settings, "agent_log_state", True)
    loop.run_cycle()
    e = lines()[-1]
    assert e["state"].startswith("market: XAU-HL") and "[volatility]" in e["state"]


def test_unreadable_market_data_is_logged_as_an_error_line(api, fakechain, cycle, monkeypatch):
    from types import SimpleNamespace

    from app import pyth
    from app.ai import hl_readonly as hl
    from app.ai import state as ai_state

    sid = active(api, fakechain)["id"]
    monkeypatch.setattr(ai_state, "pyth", SimpleNamespace(get_price=lambda *a, **k: (_ for _ in ()).throw(pyth.PriceError("STALE_PRICE", "down", 503))))
    monkeypatch.setattr(hl, "info", lambda *a, **k: (_ for _ in ()).throw(hl.SignalsReadError("down")))
    loop.run_cycle()
    e = lines()[-1]
    assert e["error"] == "SIGNALS_UNAVAILABLE" and e["strategy_id"] == sid and "neither Pyth nor Hyperliquid" in e["message"]


def test_no_secret_ever_reaches_the_log(api, fakechain, cycle, monkeypatch, caplog):
    monkeypatch.setattr(settings, "jev_api_key", "ng-super-secret-key-999")
    monkeypatch.setattr(settings, "llm_api_key", "sk-llm-secret-888")
    monkeypatch.setattr(settings, "agent_log_state", True)
    active(api, fakechain)
    with caplog.at_level(logging.DEBUG):
        loop.run_cycle()
    blob = signal_log._path().read_text() + caplog.text
    assert "ng-super-secret-key-999" not in blob and "sk-llm-secret-888" not in blob and "secret" not in blob.lower().replace("super-secret", "")


def test_an_empty_setting_turns_the_file_off_but_not_the_console_line(api, fakechain, cycle, monkeypatch, caplog):
    monkeypatch.setattr(settings, "agent_log_file", "")
    active(api, fakechain)
    with caplog.at_level(logging.INFO, logger="sereel.agent"):
        loop.run_cycle()
    assert signal_log._path() is None and signal_log.tail() == [] and any("signals (" in r.getMessage() for r in caplog.records)


def test_the_file_rotates_and_a_write_failure_never_stops_the_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "agent_log_file", str(tmp_path / "l" / "a.jsonl"))
    monkeypatch.setattr(signal_log, "MAX_BYTES", 200)
    for i in range(12):
        signal_log._append({"strategy_id": "s", "n": i, "pad": "x" * 60})
    files = sorted(p.name for p in (tmp_path / "l").iterdir())
    assert files[0] == "a.jsonl" and "a.jsonl.1" in files and len(files) <= signal_log.KEEP + 1  # bounded: old lines roll off
    assert json.loads((tmp_path / "l" / "a.jsonl").read_text().splitlines()[-1])["n"] == 11
    monkeypatch.setattr(settings, "agent_log_file", str(tmp_path / "l" / "a.jsonl" / "nope" / "x.jsonl"))  # a path that cannot be created
    signal_log._append({"strategy_id": "s"})  # logs a warning, does not raise


def test_the_log_is_ignored_by_git_and_configured_in_the_env_example():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    assert "logs/" in (root / ".gitignore").read_text() and "AGENT_LOG_FILE=" in (root / ".env.example").read_text()


def test_the_cli_shows_the_latest_entries_filtered_by_strategy(api, fakechain, cycle):
    calls, probs = cycle
    a, b = active(api, fakechain)["id"], active(api, fakechain)["id"]
    probs.update(should_rebalance="0.31")
    loop.run_cycle()
    r = runner.invoke(cli_app, ["agent", "log", "-n", "5"])
    assert r.exit_code == 0 and a[:8] in r.output and b[:8] in r.output and "should_rebalance=0.31" in r.output and "jev 5ms" in r.output and "no rule fired" in r.output
    only = runner.invoke(cli_app, ["agent", "log", "--strategy", a[:8]])
    assert a[:8] in only.output and b[:8] not in only.output
    raw = runner.invoke(cli_app, ["agent", "log", "--raw", "-n", "1"])
    assert json.loads(raw.output.strip().splitlines()[-1])["probabilities"]["should_rebalance"] == "0.31"
    empty = signal_log._path()
    empty.unlink()
    assert "no entries yet" in runner.invoke(cli_app, ["agent", "log"]).output


def test_the_cli_says_when_the_log_is_off(monkeypatch):
    monkeypatch.setattr(settings, "agent_log_file", "")
    r = runner.invoke(cli_app, ["agent", "log"])
    assert r.exit_code == 1 and "log is off" in r.output


def realistic_jev(monkeypatch, probs):
    """A Jev that returns the full JSON the real one does (model, answers, usage), so the log can be checked against it."""
    def ask(state_text, names, client=None):
        raw = {"model": "jev-1.13.0", "answers": {n: {"type": "noul", "noul": float(probs.get(n, "0.05"))} for n in names},
               "usage": {"input_tokens": 1832, "output_tokens": 131}}
        return jev.JevResult({n: D(probs.get(n, "0.05")) for n in names}, "jev-1.13.0", 549, "Authorization: Bearer", raw["usage"], raw)
    monkeypatch.setattr(jev, "ask", ask)


def test_the_full_jev_json_is_logged_exactly_as_received_in_the_file_and_the_console(api, fakechain, cycle, monkeypatch, caplog):
    realistic_jev(monkeypatch, {"should_rebalance": "0.31", "needs_top_up_soon": "0.59"})
    sid = active(api, fakechain)["id"]
    with caplog.at_level(logging.INFO, logger="sereel.agent"):
        loop.run_cycle()
    e = lines()[-1]
    resp = e["jev"]["response"]
    assert set(resp) == {"model", "answers", "usage"} and resp["model"] == "jev-1.13.0" and resp["usage"] == {"input_tokens": 1832, "output_tokens": 131}
    assert resp["answers"]["needs_top_up_soon"] == {"type": "noul", "noul": 0.59} and resp["answers"]["should_rebalance"] == {"type": "noul", "noul": 0.31}
    assert e["jev"]["latency_ms"] == 549 and e["jev"]["usage"] == resp["usage"]
    jev_lines = [r.getMessage() for r in caplog.records if "jev response:" in r.getMessage()]
    assert len(jev_lines) == 1 and jev_lines[0].startswith(f"agent {sid[:8]} jev response: ")
    assert json.loads(jev_lines[0].split("jev response: ", 1)[1]) == resp  # the console JSON is the same document, parseable as is


def test_the_rules_fallback_has_no_jev_response_to_log(api, fakechain, cycle, monkeypatch, caplog):
    monkeypatch.setattr(jev, "ask", lambda *a, **k: (_ for _ in ()).throw(jev.JevError("HTTP 503")))
    active(api, fakechain)
    with caplog.at_level(logging.INFO, logger="sereel.agent"):
        loop.run_cycle()
    assert "jev" not in lines()[-1] and not any("jev response:" in r.getMessage() for r in caplog.records)


def test_the_cli_raw_view_carries_the_full_jev_json(api, fakechain, cycle, monkeypatch):
    realistic_jev(monkeypatch, {"should_rebalance": "0.31"})
    active(api, fakechain)
    loop.run_cycle()
    raw = json.loads(runner.invoke(cli_app, ["agent", "log", "--raw", "-n", "1"]).output.strip().splitlines()[-1])
    assert raw["jev"]["response"]["answers"]["should_rebalance"]["noul"] == 0.31


class FakeSched:
    def __init__(self):
        self.jobs = []

    def add_job(self, fn, trigger, **kw):
        self.jobs.append((trigger, kw))


def test_startup_says_whether_the_agent_is_on_and_the_first_cycle_runs_within_seconds(monkeypatch, caplog):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "agent_enabled", False)
    s = FakeSched()
    with caplog.at_level(logging.INFO, logger="sereel.agent"):
        loop.register(s)
    assert s.jobs == [] and any("agent is OFF (AGENT_ENABLED=false)" in r.getMessage() and "Jev is never called" in r.getMessage() for r in caplog.records)
    caplog.clear()
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "agent_interval_s", 60)
    with caplog.at_level(logging.INFO, logger="sereel.agent"):
        loop.register(s)
    (trigger, kw), = s.jobs
    assert trigger == "interval" and kw["seconds"] == 60 and kw["id"] == "agent-cycle"
    assert timedelta(0) < kw["next_run_time"] - datetime.now(timezone.utc) <= timedelta(seconds=loop.FIRST_CYCLE_DELAY_S)  # not a whole interval's wait
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "agent is ON: a cycle runs every 60s" in msg and "whether or not anything is done" in msg and "agent_signals.jsonl" in msg


def test_the_chatty_libraries_are_quieted_so_the_agent_lines_stand_out():
    import cli.sereel_cli  # noqa: F401  (importing it configures logging, as `sereel serve` does)
    for name in ("httpx", "httpcore", "apscheduler.executors.default"):
        assert logging.getLogger(name).level == logging.WARNING, name
    assert logging.getLogger("sereel.agent").level == logging.NOTSET  # the service's own loggers are left alone: they still print at the root's INFO
