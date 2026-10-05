from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import main
from app.config import settings
from app.errors import ServiceError


def client():
    app = FastAPI()
    # reuse the real error handlers on a throwaway app
    for exc, handler in main.app.exception_handlers.items():
        app.add_exception_handler(exc, handler)

    class Body(BaseModel):
        n: int

    @app.get("/secret", dependencies=main.auth)
    def secret():
        return {"ok": True}

    @app.post("/echo", dependencies=main.auth)
    def echo(b: Body):
        return b

    @app.get("/boom", dependencies=main.auth)
    def boom():
        raise ServiceError("PRICE_DEVIATION", "mark off", 400)

    @app.get("/crash", dependencies=main.auth)
    def crash():
        raise RuntimeError("secret internals")

    return TestClient(app, raise_server_exceptions=False)


H = {"X-Sereel-Key": "test-key"}


def test_auth_required_and_fails_closed(monkeypatch):
    c = client()
    monkeypatch.setattr(settings, "api_key", "test-key")
    assert c.get("/secret").status_code == 401
    assert c.get("/secret", headers={"X-Sereel-Key": "wrong"}).status_code == 401
    assert c.get("/secret", headers=H).json() == {"ok": True}
    monkeypatch.setattr(settings, "api_key", "")  # no key configured: nothing is authorised, not even an empty header
    assert c.get("/secret", headers={"X-Sereel-Key": ""}).status_code == 401 and c.get("/secret", headers=H).status_code == 401


def test_error_bodies_use_v4_error_field_plus_code(monkeypatch):
    monkeypatch.setattr(settings, "api_key", "test-key")
    c = client()
    assert c.get("/secret").json() == {"error": "missing or invalid X-Sereel-Key", "code": "UNAUTHORIZED"}
    assert c.get("/boom", headers=H).json() == {"error": "mark off", "code": "PRICE_DEVIATION"}
    r = c.post("/echo", headers=H, json={"n": "x"})
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and r.json()["error"].startswith("n:")
    r = c.get("/nope", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND" and "error" in r.json()
    r = c.get("/crash", headers=H)
    assert r.status_code == 500 and r.json() == {"error": "unexpected error", "code": "INTERNAL"}  # no internals leaked


def test_health_is_open_and_reports_everything():
    with TestClient(main.app) as c:  # runs lifespan: simulated venue + in-memory DB
        r = c.get("/health")
        assert r.status_code == 200
        h = r.json()
        assert h["venue"] == "simulated" and h["hyperliquid"]["network"] == "testnet" and h["solana"]["network"] == "devnet"
        assert h["active_strategies"] == 0 and h["active_schedules"] == 0 and h["version"] == main.VERSION
        assert set(h) >= {"funding_address", "stablecoin_mint", "hyperliquid", "solana"}
        assert "margin" in h["hyperliquid"]


def test_mainnet_refused_at_startup_unless_allowed(monkeypatch):
    import pytest

    monkeypatch.setattr(settings, "hl_api_url", "https://api.hyperliquid.xyz")
    monkeypatch.setattr(settings, "allow_mainnet", False)
    with pytest.raises(RuntimeError, match="ALLOW_MAINNET"):
        with TestClient(main.app):
            pass
    monkeypatch.setattr(settings, "allow_mainnet", True)
    with TestClient(main.app) as c:  # allowed when explicitly enabled
        assert c.get("/health").status_code == 200
