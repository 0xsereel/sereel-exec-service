from decimal import Decimal

import pytest
import yaml
from fastapi.testclient import TestClient
from solders.keypair import Keypair
from typer.testing import CliRunner

from app import main
from app import solana_client as sol
from app.config import settings
from cli import payouts
from cli.sereel_cli import app as cli_app

D = Decimal
WALLET, OTHER = str(Keypair().pubkey()), str(Keypair().pubkey())
H = {"X-Sereel-Key": "test-key"}
runner = CliRunner()


@pytest.fixture()
def chain(monkeypatch):
    sent = []
    monkeypatch.setattr(sol, "pay", lambda to, amount, memo=None, source=None: (sent.append((to, amount, memo)), f"sig{len(sent)}")[1])
    return sent


@pytest.fixture()
def client(monkeypatch, chain):
    monkeypatch.setattr(settings, "api_key", "test-key")
    monkeypatch.setattr(settings, "stablecoin_mint", "MintAddr")
    with TestClient(main.app) as c:
        yield c


# ---- API --------------------------------------------------------------------

def test_every_payments_route_needs_the_key(client):
    for method, path in [("get", "/payments/mint"), ("post", "/payments/send"), ("get", "/payments/schedules"),
                         ("post", "/payments/schedules"), ("patch", "/payments/schedules/x"),
                         ("delete", "/payments/schedules/x"), ("get", "/payments/history")]:
        assert getattr(client, method)(path).status_code == 401, path


def test_mint_and_send(client, chain):
    assert client.get("/payments/mint", headers=H).json() == {"mint": "MintAddr", "decimals": 6}
    r = client.post("/payments/send", headers=H, json={"to": WALLET, "amount_usd": 12.5, "memo": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["amount_usd"] == 12.5 and isinstance(body["amount_usd"], float) and body["signature"] == "sig1"
    assert body["status"] == "sent" and body["created_at"].endswith("Z") and chain == [(WALLET, D("12.5"), "hi")]


def test_send_errors_use_the_v4_body(client):
    r = client.post("/payments/send", headers=H, json={"to": "nope", "amount_usd": 1})
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and "valid Solana address" in r.json()["error"]
    assert client.post("/payments/send", headers=H, json={"to": WALLET}).status_code == 400  # missing field


def test_schedule_lifecycle(client):
    body = {"name": "rev", "to": WALLET, "interval_seconds": 60, "amount_mode": "fixed", "amount_usd": 1000,
            "memo_template": "Revenue payment {seq}"}
    r = client.post("/payments/schedules", headers=H, json=body)
    assert r.status_code == 200
    sc = r.json()
    assert sc["status"] == "active" and sc["amount_usd"] == 1000 and sc["payments_made"] == 0 and sc["next_run"].endswith("Z")
    assert client.post("/payments/schedules", headers=H, json=body).status_code == 409
    listed = client.get("/payments/schedules", headers=H).json()
    assert isinstance(listed, list) and listed[0]["id"] == sc["id"] and listed[0]["total_paid_usd"] == 0
    assert client.patch(f"/payments/schedules/{sc['id']}", headers=H, json={"status": "paused"}).json()["status"] == "paused"
    assert client.patch(f"/payments/schedules/{sc['id']}", headers=H, json={"status": "active"}).json()["status"] == "active"
    assert client.patch(f"/payments/schedules/{sc['id']}", headers=H, json={"status": "stopped"}).status_code == 400
    assert client.delete(f"/payments/schedules/{sc['id']}", headers=H).json()["status"] == "stopped"
    assert client.patch(f"/payments/schedules/{sc['id']}", headers=H, json={"status": "active"}).status_code == 409  # final
    assert client.delete("/payments/schedules/nope", headers=H).status_code == 404


def test_history_filters_by_recipient(client):
    for to in (WALLET, WALLET, OTHER):
        client.post("/payments/send", headers=H, json={"to": to, "amount_usd": 1})
    assert len(client.get("/payments/history", headers=H).json()) == 3
    only = client.get("/payments/history", headers=H, params={"to": OTHER}).json()
    assert len(only) == 1 and only[0]["to"] == OTHER


# ---- CLI --------------------------------------------------------------------

@pytest.mark.parametrize("text,secs", [("30s", 30), ("1m", 60), ("5m", 300), ("1h", 3600), ("1d", 86400), ("1 day", 86400),
                                       ("90", 90), ("90s", 90), ("2h", 7200)])
def test_parse_interval(text, secs):
    assert payouts.parse_interval(text) == secs


def test_parse_interval_rejects_junk():
    import typer

    with pytest.raises(typer.BadParameter):
        payouts.parse_interval("soon")


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(payouts, "ROOT", tmp_path)
    return tmp_path


def test_new_non_interactive_saves_a_profile_matching_the_spec_example(root):
    r = runner.invoke(cli_app, ["payouts", "new", "--name", "fund-b", "--fund-id", "F9", "--to", WALLET, "--interval", "300",
                                "--mode", "fixed", "--amount", "2500", "--yes", "--no-start"])
    assert r.exit_code == 0, r.output
    prof = yaml.safe_load((root / "profiles" / "fund-b.yaml").read_text())
    assert prof == {"name": "fund-b", "fund_id": "F9", "to": WALLET, "interval_seconds": 300, "amount_mode": "fixed",
                    "amount_usd": 2500.0, "memo_template": "Revenue payment {seq}"}


def test_new_non_interactive_priced_with_stop_conditions(root):
    r = runner.invoke(cli_app, ["payouts", "new", "--name", "gold", "--fund-id", "F", "--to", WALLET, "--interval", "1m",
                                "--mode", "priced", "--feed-id", "ab" * 32, "--units", "20", "--jitter", "5",
                                "--max-payments", "10", "--end-at", "2030-01-01T00:00:00Z", "--yes", "--no-start"])
    assert r.exit_code == 0, r.output
    prof = yaml.safe_load((root / "profiles" / "gold.yaml").read_text())
    assert prof["pricing"] == {"pyth_feed_id": "ab" * 32, "units_per_payment": 20.0, "units_jitter_pct": 5.0}
    assert prof["max_payments"] == 10 and prof["end_at"].startswith("2030-01-01T00:00:00") and "{units}" in prof["memo_template"]


def test_new_rejects_bad_input_without_saving(root):
    common = ["payouts", "new", "--name", "x", "--fund-id", "F", "--interval", "1m", "--mode", "fixed", "--amount", "1", "--yes"]
    assert runner.invoke(cli_app, common + ["--to", "not-a-wallet"]).exit_code == 1
    assert runner.invoke(cli_app, [*common[:-1], "--yes", "--to", WALLET, "--memo", "{bogus}"]).exit_code == 1
    assert runner.invoke(cli_app, ["payouts", "new", "--name", "../evil", "--fund-id", "F", "--to", WALLET, "--interval", "1m",
                                   "--mode", "fixed", "--amount", "1", "--yes"]).exit_code != 0
    assert not (root / "profiles").exists() or not list((root / "profiles").glob("*"))


def test_new_interactive_follows_the_spec_prompt_order(root):
    inputs = "fund-b\nF1\n" + WALLET + "\n5m\nfixed\n2500\n\nnever\ny\nn\n"  # name, fund, wallet, freq, mode, amt, memo(default), stop, save, start?
    r = runner.invoke(cli_app, ["payouts", "new"], input=inputs)
    assert r.exit_code == 0, r.output
    order = [r.output.index(s) for s in ("Profile name", "Fund ID", "Revenue wallet", "How often", "Amount mode", "Memo template",
                                         "Stop condition", "Summary", "Start now")]
    assert order == sorted(order)
    assert yaml.safe_load((root / "profiles" / "fund-b.yaml").read_text())["interval_seconds"] == 300


def test_run_registers_once_then_list_pause_resume_stop_by_name(root):
    runner.invoke(cli_app, ["payouts", "new", "--name", "rev", "--fund-id", "F", "--to", WALLET, "--interval", "60", "--mode",
                            "fixed", "--amount", "10", "--yes", "--no-start"])
    assert "started" in runner.invoke(cli_app, ["payouts", "run", "rev"]).output
    again = runner.invoke(cli_app, ["payouts", "run", "rev"])
    assert again.exit_code == 0 and "already registered" in again.output  # restart without prompts, no duplicate
    assert "rev" in runner.invoke(cli_app, ["payouts", "list"]).output
    assert "paused" in runner.invoke(cli_app, ["payouts", "pause", "rev"]).output
    assert "resumed" in runner.invoke(cli_app, ["payouts", "resume", "rev"]).output
    assert "stopped" in runner.invoke(cli_app, ["payouts", "stop", "rev"]).output
    r = runner.invoke(cli_app, ["payouts", "pause", "rev"])  # a stopped schedule cannot be paused
    assert r.exit_code == 1 and "CONFLICT" in r.output
    assert runner.invoke(cli_app, ["payouts", "pause", "nope"]).exit_code == 1
    assert runner.invoke(cli_app, ["payouts", "run", "missing-profile"]).exit_code == 1


def test_send_one_off(root, chain):
    r = runner.invoke(cli_app, ["payouts", "send", "--to", WALLET, "--amount", "7.25", "--memo", "test"])
    assert r.exit_code == 0 and "sig1" in r.output and chain == [(WALLET, D("7.25"), "test")]
    assert runner.invoke(cli_app, ["payouts", "send", "--to", "bad", "--amount", "1"]).exit_code == 1
