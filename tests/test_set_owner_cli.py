import json

import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select
from typer.testing import CliRunner

from app.db import engine
from app.models import Action, Strategy
from app.strategies import attest as att
from auth_helpers import Signer
from cli.sereel_cli import app
from test_auth import SQUADS_ADDR, FIXTURE
from test_strategies import BODY, create, get, rows

runner = CliRunner()


def ownerless(api):
    """A strategy from before ownership existed (created under the dev bypass, so no owner)."""
    from app.config import settings

    settings.dev_auth_bypass = True
    try:
        s = create(api, owner_pubkey=None)
    finally:
        settings.dev_auth_bypass = False
    assert s["owner_pubkey"] is None and s["owner_multisig"] is None
    return s


def test_set_owner_binds_a_pubkey_and_attests_it_as_operator(api, fakechain):
    s = ownerless(api)
    me = Signer().pubkey
    r = runner.invoke(app, ["strategies", "set-owner", s["id"], "--pubkey", me])
    assert r.exit_code == 0, r.output
    got = get(api, s["id"])
    assert got["owner_pubkey"] == me and got["owner_multisig"] is None
    (act,) = [a for a in rows(Action) if a.action == "set_owner"]
    assert act.record["bound_by"] == "operator" and act.record["to"]["owner_pubkey"] == me and act.record["from"]["owner_pubkey"] is None
    m = json.loads(fakechain.memos[-1])
    assert m["a"] == "set_owner" and m["id"] == s["id"] and m["h"] == att.record_hash(act.record) and act.attestation_sig
    assert "explorer.solana.com" in r.output


def test_set_owner_can_bind_a_squads_multisig_but_only_a_real_one(api, fakechain):
    s = ownerless(api)
    bogus = runner.invoke(app, ["strategies", "set-owner", s["id"], "--multisig", str(Keypair().pubkey())])
    assert bogus.exit_code == 1 and "not an account" in bogus.output
    fakechain.accounts[SQUADS_ADDR] = {"owner": FIXTURE["owner"], "data": [FIXTURE["data_b64"], "base64"]}
    ok = runner.invoke(app, ["strategies", "set-owner", s["id"], "--multisig", SQUADS_ADDR])
    assert ok.exit_code == 0, ok.output
    assert get(api, s["id"])["owner_multisig"] == SQUADS_ADDR


def test_set_owner_refuses_a_strategy_that_already_has_an_owner(api, fakechain):
    s = create(api)  # created with an owner
    before = fakechain.memos[:]
    r = runner.invoke(app, ["strategies", "set-owner", s["id"], "--pubkey", Signer().pubkey])
    assert r.exit_code == 1 and "CONFLICT" in r.output and "already has an owner" in " ".join(r.output.split())
    assert get(api, s["id"])["owner_pubkey"] == BODY["owner_pubkey"] and fakechain.memos == before  # untouched, nothing attested


def test_set_owner_argument_and_lookup_errors(api, fakechain):
    s = ownerless(api)
    assert runner.invoke(app, ["strategies", "set-owner", s["id"]]).exit_code == 1  # neither flag
    both = runner.invoke(app, ["strategies", "set-owner", s["id"], "--pubkey", Signer().pubkey, "--multisig", SQUADS_ADDR])
    assert both.exit_code == 1 and "exactly one" in both.output
    assert runner.invoke(app, ["strategies", "set-owner", s["id"], "--pubkey", "nope"]).exit_code == 1
    missing = runner.invoke(app, ["strategies", "set-owner", "no-such-id", "--pubkey", Signer().pubkey])
    assert missing.exit_code == 1 and "NOT_FOUND" in missing.output
    assert get(api, s["id"])["owner_pubkey"] is None


def test_there_is_no_api_route_to_set_an_owner(api):
    s = ownerless(api)
    for method, path in (("post", f"/strategies/{s['id']}/set-owner"), ("put", f"/strategies/{s['id']}/owner"),
                         ("patch", f"/strategies/{s['id']}")):
        r = getattr(api, method)(path, json={"owner_pubkey": Signer().pubkey})
        assert r.status_code in (404, 405, 401, 403, 400), (method, path, r.status_code)
    assert get(api, s["id"])["owner_pubkey"] is None
    # and the one signed owner route cannot bind an owner to an ownerless strategy either: nobody can sign for it
    me = Signer()
    params = {"owner_pubkey": me.pubkey}
    body = {**params, "authorization": me.authorization("change_owner", s["id"], params)}
    r = api.post(f"/strategies/{s['id']}/owner", json=body)
    assert r.status_code == 403 and "no owner bound" in r.json()["error"]
