import json
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from typer.testing import CliRunner

from app import solana_client as sol
from app.config import settings
from cli import setup
from cli.sereel_cli import app

D = Decimal
runner = CliRunner()
NAMES = {"funding": "funding_keypair", "attest": "attest_keypair",
         "mint_authority": "mint_authority_keypair", "payment_source": "payment_source_keypair"}


@pytest.fixture()
def keys(tmp_path, monkeypatch):
    for name, attr in NAMES.items():
        monkeypatch.setattr(settings, attr, str(tmp_path / "keys" / f"{name}.json"))
    return tmp_path / "keys"


def pubkeys():
    return {n: str(sol.load_keypair(getattr(settings, a)).pubkey()) for n, a in NAMES.items()}


def test_ensure_keys_creates_all_with_private_permissions(keys):
    assert set(setup.ensure_keys().values()) == {"created"}
    assert len(list(keys.glob("*.json"))) == 4 and all(oct(p.stat().st_mode & 0o777) == "0o600" for p in keys.glob("*.json"))


def test_existing_keys_are_never_overwritten_without_force(keys):
    setup.ensure_keys()
    before = pubkeys()
    assert set(setup.ensure_keys().values()) == {"kept"}
    assert pubkeys() == before


def test_force_replaces_but_moves_the_old_key_aside(keys):
    setup.ensure_keys()
    before = pubkeys()
    old_bytes = (keys / "funding.json").read_bytes()
    assert set(setup.ensure_keys(force=True).values()) == {"replaced"}
    after = pubkeys()
    assert all(after[n] != before[n] for n in NAMES)
    backups = list(keys.glob("funding.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == old_bytes  # recoverable, not deleted


def fake_chain(monkeypatch, balances):
    sent = []
    monkeypatch.setattr(sol, "sol_balance", lambda pk: balances.get(str(pk), D(0)))

    def transfer(src, to, amt):
        sent.append((str(to), amt))
        balances[str(to)] = balances.get(str(to), D(0)) + amt
        return "sig"

    monkeypatch.setattr(sol, "transfer_sol", transfer)
    return sent


def test_distribute_sends_only_shortfalls(keys, monkeypatch):
    setup.ensure_keys()
    pk = pubkeys()
    bal = {pk["funding"]: D(1), pk["attest"]: D("0.05")}  # attest already holds some; 0.7 spendable after the reserve
    sent = fake_chain(monkeypatch, bal)
    assert setup.distribute_sol() == {"attest": "sent", "mint_authority": "sent", "payment_source": "sent"}
    assert dict(sent) == {pk["attest"]: D("0.15"), pk["mint_authority"]: D("0.2"), pk["payment_source"]: D("0.2")}
    sent.clear()
    assert setup.distribute_sol() == {"attest": "ok", "mint_authority": "ok", "payment_source": "ok"} and sent == []  # idempotent


def test_distribute_keeps_the_funding_reserve(keys, monkeypatch):
    setup.ensure_keys()
    pk = pubkeys()
    sent = fake_chain(monkeypatch, {pk["funding"]: D("0.6")})  # 0.3 spendable: one 0.2 transfer fits, the next does not
    res = setup.distribute_sol()
    assert res["attest"] == "sent" and res["mint_authority"].startswith("skipped") and res["payment_source"].startswith("skipped")
    assert [a for a, _ in sent] == [pk["attest"]]


def test_distribute_does_nothing_when_funding_is_too_small(keys, monkeypatch):
    setup.ensure_keys()
    sent = fake_chain(monkeypatch, {pubkeys()["funding"]: D("0.3")})
    assert all(v.startswith("skipped") for v in setup.distribute_sol().values()) and sent == []


def test_set_env_value_keeps_comments_and_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("A=1\nSTABLECOIN_MINT=                     # mock USDC; created by init\nB=2\n")
    setup.set_env_value("STABLECOIN_MINT", "Mint111", env)
    assert env.read_text() == "A=1\nSTABLECOIN_MINT=Mint111  # mock USDC; created by init\nB=2\n"
    setup.set_env_value("NEW", "x", env)
    assert env.read_text().endswith("B=2\nNEW=x\n")
    setup.set_env_value("STABLECOIN_MINT", "Mint222", env)  # replaces a previous value too
    assert "STABLECOIN_MINT=Mint222" in env.read_text() and "Mint111" not in env.read_text()


def test_init_prints_only_the_funding_address_and_waits_for_funds(keys, monkeypatch):
    monkeypatch.setattr(sol, "sol_balance", lambda pk: D(0))
    out = runner.invoke(app, ["init", "--no-wait"]).output
    pk = pubkeys()
    assert pk["funding"] in out and "faucet.solana.com" in out
    assert not any(pk[n] in out for n in ("attest", "mint_authority", "payment_source"))  # others stay unprinted


def test_init_full_flow_distributes_creates_mint_and_reminds_about_backup(keys, monkeypatch, tmp_path):
    bal = {}
    sent = fake_chain(monkeypatch, bal)
    runner.invoke(app, ["init", "--no-wait"])  # first run: creates keys, balance 0, exits
    pk = pubkeys()
    bal[pk["funding"]] = D(2)
    monkeypatch.setattr(settings, "stablecoin_mint", "")
    monkeypatch.setattr(sol, "create_mint", lambda payer, auth: Keypair().pubkey())
    env_calls = []
    monkeypatch.setattr(setup, "set_env_value", lambda k, v, env=None: env_calls.append((k, v)))
    res = runner.invoke(app, ["init", "--poll-s", "0"])
    assert res.exit_code == 0, res.output
    assert {a for a, _ in sent} == {pk["attest"], pk["mint_authority"], pk["payment_source"]}
    assert env_calls and env_calls[0][0] == "STABLECOIN_MINT"
    assert "outside this repository" in res.output and "keys/" in res.output and ".env" in res.output
    assert not any(pk[n] in res.output for n in ("attest", "mint_authority", "payment_source"))


def test_init_refuses_mainnet(keys, monkeypatch):
    monkeypatch.setattr(settings, "hl_api_url", "https://api.hyperliquid.xyz")
    monkeypatch.setattr(settings, "allow_mainnet", False)
    r = runner.invoke(app, ["init", "--no-wait"])
    assert r.exit_code == 1 and "ALLOW_MAINNET" in r.output
    assert not keys.exists()  # refused before any key was generated


def test_force_flag_reaches_the_command(keys, monkeypatch):
    monkeypatch.setattr(sol, "sol_balance", lambda pk: D(0))
    runner.invoke(app, ["init", "--no-wait"])
    before = pubkeys()
    runner.invoke(app, ["init", "--no-wait"])
    assert pubkeys() == before  # plain re-run keeps keys
    out = runner.invoke(app, ["init", "--no-wait", "--force"]).output
    assert pubkeys()["funding"] != before["funding"] and "moved aside" in out
