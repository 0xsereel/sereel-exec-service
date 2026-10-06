import pytest
from typer.testing import CliRunner

from app.config import settings
from app.errors import ServiceError
from cli import sereel_cli
from cli.sereel_cli import app

runner = CliRunner()


class FakeMaker:
    instances = []

    def __init__(self):
        self.events = []
        FakeMaker.instances.append(self)

    def run(self, manage_pid=True):
        self.events.append(("run", manage_pid))

    def stop(self):
        self.events.append(("stop",))


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    import uvicorn

    FakeMaker.instances = []
    maker_args = {}
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(sereel_cli, "ROOT", tmp_path)

    def build(market, **kw):
        maker_args.update(market=market, **kw)
        return FakeMaker()

    monkeypatch.setattr(sereel_cli, "_build_mm", build)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    return maker_args, calls


def test_serve_without_mm_starts_no_maker(wired):
    args, calls = wired
    assert runner.invoke(app, ["serve", "--port", "9000"]).exit_code == 0
    assert FakeMaker.instances == [] and calls[0][1]["port"] == 9000


def test_serve_mm_runs_the_maker_embedded_without_a_pidfile_and_stops_it_after_the_server(wired):
    args, calls = wired
    r = runner.invoke(app, ["serve", "--mm", "--mm-size", "0.04", "--mm-center", "mark", "--mm-flatten-wait", "9"])
    assert r.exit_code == 0, r.output
    (maker,) = FakeMaker.instances
    assert maker.events == [("run", False), ("stop",)]  # manage_pid=False: `mm stop` must never signal the server
    assert args == {"market": "XAU-HL", "size": 0.04, "center": "mark", "flatten_wait": 9, "save": False, "interactive": False}
    assert calls and "market maker running inside the server" in r.output


def test_the_maker_is_stopped_even_if_the_server_crashes(wired, monkeypatch):
    import uvicorn

    def boom(*a, **k):
        raise RuntimeError("server died")

    monkeypatch.setattr(uvicorn, "run", boom)
    r = runner.invoke(app, ["serve", "--mm"])
    assert r.exit_code != 0
    assert ("stop",) in FakeMaker.instances[0].events  # quotes are cancelled and inventory flattened either way


def test_serve_mm_refuses_to_start_when_the_maker_cannot(monkeypatch):
    import uvicorn

    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    monkeypatch.setattr(sereel_cli, "_build_mm", lambda *a, **k: (_ for _ in ()).throw(ServiceError("MM_MAINNET_REFUSED", "testnet only")))
    r = runner.invoke(app, ["serve", "--mm"])
    assert r.exit_code == 1 and "MM_MAINNET_REFUSED" in r.output


def test_serve_still_requires_an_api_key_and_safe_networks(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "")
    assert runner.invoke(app, ["serve"]).exit_code == 1
    monkeypatch.setattr(settings, "api_key", "k")
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "allow_mainnet", True)
    r = runner.invoke(app, ["serve"])
    assert r.exit_code == 1 and "DEV_AUTH_BYPASS" in r.output  # the bypass + mainnet combination is refused here too


def test_the_embedded_maker_really_does_not_touch_the_pidfile(monkeypatch, tmp_path):
    from test_mm import bare
    from test_mm_startup import Book, Exch, mm_with, Clock
    from app.mm import market_maker as mmod

    pid = tmp_path / "mm.pid"
    monkeypatch.setattr(mmod, "pidfile", lambda m: pid)
    clock = Clock()
    monkeypatch.setattr(mmod.time, "time", clock)
    monkeypatch.setattr(mmod.time, "sleep", clock.sleep)
    mm = mm_with(Book(inv="0"), clock)
    mm.tick = lambda: (mm.stop(), None)[1]
    mm.run(manage_pid=False)
    assert not pid.exists()  # never written
    pid.write_text("123")
    mm2 = mm_with(Book(inv="0"), clock)
    mm2.tick = lambda: (mm2.stop(), None)[1]
    mm2.run(manage_pid=False)
    assert pid.read_text() == "123"  # and never deleted either
