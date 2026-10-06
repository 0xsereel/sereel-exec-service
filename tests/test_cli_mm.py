import signal

from typer.testing import CliRunner

from app.config import settings
from app.mm import market_maker as mmod
from cli import sereel_cli
from cli.sereel_cli import app

runner = CliRunner()


def test_mm_run_refuses_mainnet(monkeypatch):
    monkeypatch.setattr(settings, "hl_api_url", "https://api.hyperliquid.xyz")
    monkeypatch.setattr(settings, "allow_mainnet", True)
    r = runner.invoke(app, ["mm", "run", "--market", "XAU-HL", "--size", "0.03"])
    assert r.exit_code == 1 and "MM_MAINNET_REFUSED" in r.output


def test_mm_run_rejects_unknown_market_and_out_of_range_size():
    r = runner.invoke(app, ["mm", "run", "--market", "NOPE", "--size", "0.03"])
    assert r.exit_code == 1 and "unknown market" in r.output
    r = runner.invoke(app, ["mm", "run", "--market", "XAU-HL", "--size", "0.5", "--spread-bps", "10", "--levels", "3",
                            "--center", "oracle", "--no-save"])
    assert r.exit_code == 1 and "MM_SIZE_OUT_OF_RANGE" in r.output


def test_mm_run_builds_config_wires_sigterm_and_saves_profile(monkeypatch, tmp_path):
    seen = {}

    class FakeMM:
        def __init__(self, market, cfg):
            seen["cfg"], seen["market"] = cfg, market

        def run(self):
            seen["ran"] = True

        def stop(self):
            seen["stopped"] = True

    monkeypatch.setattr(mmod, "MarketMaker", FakeMM)
    monkeypatch.setattr(sereel_cli, "ROOT", tmp_path)
    handlers = {}
    monkeypatch.setattr(sereel_cli.signal, "signal", lambda sig, h: handlers.__setitem__(sig, h))
    r = runner.invoke(app, ["mm", "run", "--market", "XAU-HL", "--size", "0.04", "--spread-bps", "8", "--levels", "2",
                            "--center", "pyth"])
    assert r.exit_code == 0 and seen["ran"]
    cfg = seen["cfg"]
    assert (cfg.size, cfg.spread_bps, cfg.levels, cfg.center) == (cfg.size.__class__("0.04"), 8, 2, "pyth")
    handlers[signal.SIGTERM]()  # what `mm stop` triggers
    assert seen["stopped"]
    assert (tmp_path / "profiles" / "mm-XAU-HL.yaml").exists()
    # a second run with no flags reuses the saved profile without prompting
    seen.clear()
    assert runner.invoke(app, ["mm", "run", "--market", "XAU-HL"]).exit_code == 0
    assert seen["cfg"].size == cfg.size and seen["cfg"].center == "pyth"


def test_mm_stop_reports_when_nothing_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(mmod, "pidfile", lambda m: tmp_path / "none.pid")
    r = runner.invoke(app, ["mm", "stop", "--market", "XAU-HL"])
    assert r.exit_code == 1 and "no running market maker" in r.output


def test_mm_stop_signals_running_process(monkeypatch, tmp_path):
    (tmp_path / "p.pid").write_text("999")
    monkeypatch.setattr(mmod, "pidfile", lambda m: tmp_path / "p.pid")
    sent = []
    monkeypatch.setattr(mmod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    r = runner.invoke(app, ["mm", "stop", "--market", "XAU-HL"])
    assert r.exit_code == 0 and sent == [(999, signal.SIGTERM)] and "flattening" in r.output
