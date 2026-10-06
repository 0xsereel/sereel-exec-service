import json
from decimal import Decimal

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from app.db import engine
from app.models import Action, PnlSnapshot
from app.state import state
from app.strategies import attest as att
from app.strategies import service
from cli.sereel_cli import app
from test_step1 import M, active, get, snapshots
from test_strategies import rows

D = Decimal
runner = CliRunner(env={"COLUMNS": "200"})
REASON = "Margin leak: a second strategy was credited 82.00 but 81.567556 reached the dex; the difference came out of this pot."


def adjust(sid, amount="-0.432444", reason=REASON, yes=True):
    args = ["strategies", "adjust-ledger", sid, "--realized", amount, "--reason", reason] + (["--yes"] if yes else [])
    return runner.invoke(app, args)


def test_it_books_the_adjustment_into_realized_pnl_and_hedge_pnl(api, fakechain):
    s = active(api, fakechain)
    before = api.get(f"/strategies/{s['id']}/value").json()
    r = adjust(s["id"])
    assert r.exit_code == 0, r.output
    after = api.get(f"/strategies/{s['id']}/value").json()
    assert after["realized_pnl_usd"] == pytest.approx(before["realized_pnl_usd"] - 0.432444)
    assert after["hedge_pnl_usd"] == pytest.approx(before["hedge_pnl_usd"] - 0.432444)  # the strategy really is poorer by that much
    assert get(api, s["id"])["position"]["realized_pnl_usd"] == pytest.approx(-0.432444)
    assert "booked" in r.output and "explorer.solana.com" in r.output


def test_it_is_attested_as_an_operator_action_with_the_full_record(api, fakechain):
    s = active(api, fakechain)
    adjust(s["id"])
    (a,) = [x for x in rows(Action) if x.action == "ledger_adjustment"]
    rec = a.record
    assert rec["booked_by"] == "operator" and rec["reason"] == REASON and rec["field"] == "realized_pnl_usd"
    assert D(rec["delta"]) == D("-0.432444") and D(rec["before"]) == 0 and D(rec["after"]) == D("-0.432444")
    m = json.loads(fakechain.memos[-1])
    assert m["a"] == "ledger_adjustment" and m["id"] == s["id"] and m["h"] == att.record_hash(a.record) and a.attestation_sig
    assert a.signer_public_key is None  # an operator action, not a signed owner action
    assert [x.cause for x in snapshots(s["id"])][-1] == "ledger_adjustment"
    h = api.get(f"/strategies/{s['id']}/history").json()
    assert h[-1]["type"] == "ledger_adjustment" and h[-1]["details"]["reason"] == REASON  # visible to the manager's UI


def test_it_fixes_the_reconciliation_gap_it_was_made_for(api, fakechain):
    """Re-create the live situation: the venue holds 0.432444 less than the ledger says, then book exactly that."""
    s = active(api, fakechain, amount=200)
    state.venue._cash[M] -= D("0.432444")
    r = api.get("/health").json()["reconciliation"]
    assert r["ok"] is False and r["difference_usd"] == pytest.approx(-0.432444, abs=1e-6)
    assert adjust(s["id"]).exit_code == 0
    r = api.get("/health").json()["reconciliation"]
    assert r["ok"] is True and abs(r["difference_usd"]) < 1e-6


def test_a_reason_is_mandatory_and_must_say_something(api, fakechain):
    s = active(api, fakechain)
    for bad in ("", "   ", "fix", "x" * 19):
        r = adjust(s["id"], reason=bad)
        assert r.exit_code == 1 and "reason of at least 20 characters" in " ".join(r.output.split()), bad
    assert [x for x in rows(Action) if x.action == "ledger_adjustment"] == []
    assert get(api, s["id"])["position"]["realized_pnl_usd"] == 0


def test_the_same_reason_cannot_be_booked_twice(api, fakechain):
    s = active(api, fakechain)
    assert adjust(s["id"]).exit_code == 0
    again = adjust(s["id"])
    assert again.exit_code == 1 and "CONFLICT" in again.output and "booked once" in " ".join(again.output.split())
    assert get(api, s["id"])["position"]["realized_pnl_usd"] == pytest.approx(-0.432444)  # not -0.8649
    assert adjust(s["id"], amount="-0.1", reason="A different, separately explained correction of ten cents.").exit_code == 0


def test_a_confirmation_is_asked_for_and_declining_books_nothing(api, fakechain):
    s = active(api, fakechain)
    r = runner.invoke(app, ["strategies", "adjust-ledger", s["id"], "--realized", "-1", "--reason", REASON], input="n\n")
    assert r.exit_code == 1 and "cancelled" in r.output
    assert get(api, s["id"])["position"]["realized_pnl_usd"] == 0
    ok = runner.invoke(app, ["strategies", "adjust-ledger", s["id"], "--realized", "-1", "--reason", REASON], input="y\n")
    assert ok.exit_code == 0 and get(api, s["id"])["position"]["realized_pnl_usd"] == -1


@pytest.mark.parametrize("amount", ["abc", "", "1e"])
def test_the_amount_must_be_a_number(api, fakechain, amount):
    s = active(api, fakechain)
    assert adjust(s["id"], amount=amount).exit_code == 1


def test_zero_unknown_and_finished_strategies_are_refused(api, fakechain):
    s = active(api, fakechain)
    assert "zero" in adjust(s["id"], amount="0").output
    assert "NOT_FOUND" in adjust("no-such-id").output
    from test_step2 import close
    close(api, s["id"])  # a closed strategy has nothing left to adjust
    r = adjust(s["id"], reason="A reason that is long enough to pass the check.")
    assert r.exit_code == 1 and "CONFLICT" in r.output and "closed" in r.output


def test_there_is_no_api_route_to_adjust_a_ledger(api, fakechain):
    s = active(api, fakechain)
    for method, path in (("post", f"/strategies/{s['id']}/adjust-ledger"), ("post", f"/strategies/{s['id']}/adjustments"),
                         ("patch", f"/strategies/{s['id']}"), ("put", f"/strategies/{s['id']}/ledger")):
        r = getattr(api, method)(path, json={"realized": "-1", "reason": REASON, "realized_pnl_usd": "-1"})
        assert r.status_code in (400, 401, 403, 404, 405), (method, path)
    assert get(api, s["id"])["position"]["realized_pnl_usd"] == 0
