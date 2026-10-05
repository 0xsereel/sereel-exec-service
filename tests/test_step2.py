import json
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select
from typer.testing import CliRunner

from app.config import settings
from app.db import engine
from app.models import Action, Strategy, UsedNonce, Withdrawal
from app.state import state
from app.strategies import service, withdrawals
from auth_helpers import Signer
from cli.sereel_cli import app as cli_app
from test_strategies import BODY, M, OWNER, SENDER, create, fund, get, rows

D = Decimal
DEST = str(Keypair().pubkey())
runner = CliRunner()


@pytest.fixture(autouse=True)
def caches():
    service._mark_cache.clear()
    service._liq_cache.clear()


def set_price(px):
    state.venue.price_override[M] = D(str(px))
    service._mark_cache.clear()
    service._liq_cache.clear()


def active(api, fakechain, amount=127.2):
    s = create(api)
    fund(api, fakechain, s, amount=amount)
    assert get(api, s["id"])["status"] == "active"
    return s


def auth_close(sid, dest=DEST, signer=OWNER, **kw):
    params = {"destination_wallet_address": dest}
    return params, signer.authorization("close_strategy", sid, params, **kw)


def close(api, sid, dest=DEST, signer=OWNER, **kw):
    params, a = auth_close(sid, dest, signer, **kw)
    body = {**params, "authorization": a}
    return api.request("DELETE", f"/strategies/{sid}", json=body), body


def auth_excess(sid, amount, dest=DEST, signer=OWNER, **kw):
    params = {"amount_usd": amount, "destination_wallet_address": dest}
    return params, signer.authorization("return_excess", sid, params, **kw)


def excess(api, sid, amount, dest=DEST, signer=OWNER, **kw):
    params, a = auth_excess(sid, amount, dest, signer, **kw)
    body = {"type": "return_excess", **params, "authorization": a}
    return api.post(f"/strategies/{sid}/withdrawals", json=body), body


def wd(api, sid, wid):
    r = api.get(f"/strategies/{sid}/withdrawals/{wid}")
    assert r.status_code == 200, r.text
    return r.json()


def wrow(wid):
    with Session(engine) as db:
        return db.get(Withdrawal, wid)


def actions(sid, kind):
    with Session(engine) as db:
        return [a for a in db.exec(select(Action).where(Action.strategy_id == sid, Action.action == kind)).all()]


# ---- close: the happy path ----------------------------------------------------------

def test_close_returns_the_withdrawal_at_once_and_ends_completed_with_the_money_sent(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    set_price(2640)  # the short gains: (2650 - 2640) * 0.12 = 1.2
    r, _ = close(api, sid)
    assert r.status_code == 200
    w = r.json()
    assert w["type"] == "close" and w["status"] == "requested" and w["strategy_id"] == sid  # the immediate answer
    assert w["destination_wallet_address"] == DEST and w["failure_reason"] is None and w["solana_signature"] is None
    assert set(w) == {"id", "strategy_id", "type", "amount_usd", "destination_wallet_address", "status", "failure_reason",
                      "solana_signature", "attestation_url", "created_at", "updated_at"}
    final = wd(api, sid, w["id"])  # the background machine has run by now
    fees = 0.12 * 2650 * 0.00045 + 0.12 * 2640 * 0.00045
    expected = 127.2 + 1.2 - fees
    assert final["status"] == "completed" and final["solana_signature"] == "payout1" and "explorer.solana.com" in final["attestation_url"]
    assert final["amount_usd"] == pytest.approx(expected, abs=1e-6)  # the finalized amount, not the estimate
    assert len(fakechain.payouts) == 1 and fakechain.payouts[0][0] == DEST
    assert float(fakechain.payouts[0][1]) == pytest.approx(expected, abs=1e-6) and fakechain.payouts[0][2].startswith("sereel close ")
    a = get(api, sid)
    assert a["status"] == "closed" and a["closed_at"] and a["position"] is None
    assert state.venue.position(None, M).size == 0
    assert state.venue.orders[-1]["reduce_only"] is True and state.venue.orders[-1]["is_buy"] is True  # closing never opens
    v = api.get(f"/strategies/{sid}/value").json()
    assert v["margin_usd"] == 0 and v["unrealized_pnl_usd"] == 0 and v["realized_pnl_usd"] == pytest.approx(1.2)
    assert v["hedge_pnl_usd"] == pytest.approx(1.2 - fees, abs=1e-6)  # the strategy's P&L survives the close
    assert state.venue._cash[M] == pytest.approx(0, abs=1e-6)  # everything was released from the venue balance


def test_the_state_machine_moves_through_every_status_in_order(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    seen = []
    real = withdrawals._update

    def spy(wid, **fields):
        if "status" in fields:
            seen.append(fields["status"])
        return real(wid, **fields)

    monkeypatch.setattr(withdrawals, "_update", spy)
    close(api, s["id"])
    assert seen == ["position_closed", "released", "bridging", "completed"]


def test_while_in_flight_the_strategy_is_closing_and_a_second_close_conflicts(api, fakechain):
    s = active(api, fakechain)
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)  # request only: nothing has advanced yet
    assert w.status == "requested" and get(api, s["id"])["status"] == "closing"
    r, _ = close(api, s["id"])  # a different signed request while one is in flight
    assert r.status_code == 409 and w.id in r.json()["error"]
    assert withdrawals.advance_withdrawal(w.id) == "completed" and get(api, s["id"])["status"] == "closed"


def test_the_close_is_attested_with_every_reference(api, fakechain):
    s = active(api, fakechain)
    set_price(2640)
    w = close(api, s["id"])[0].json()
    (act,) = actions(s["id"], "close")
    r = act.record
    assert r["event"] == "close" and r["withdrawal_id"] == w["id"] and r["destination"] == DEST and r["solana_signature"] == "payout1"
    assert r["close"]["oids"] and D(r["close"]["filled"]) == D("0.12") and D(r["close"]["realized_pnl_usd"]) == D("1.2")
    assert set(r["final"]) == {"margin_usd", "realized_pnl_usd", "funding_usd", "fees_usd"} and r["route"]["route"] == "mirrored"
    assert act.signer_public_key == OWNER.pubkey and act.hl_order_ids and act.solana_signature == "payout1"
    m = json.loads(fakechain.memos[-1])
    from app.strategies import attest as att
    assert m["a"] == "close" and m["id"] == s["id"] and m["h"] == att.record_hash(act.record) and act.attestation_sig
    assert wrow(w["id"]).attestation_sig == act.attestation_sig


def test_a_retried_request_returns_the_same_withdrawal_and_pays_once(api, fakechain):
    s = active(api, fakechain)
    r1, body = close(api, s["id"])
    r2 = api.request("DELETE", f"/strategies/{s['id']}", json=body)  # the client retried the identical signed request
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"] and len(fakechain.payouts) == 1
    other = {**body, "destination_wallet_address": str(Keypair().pubkey())}  # same nonce, different content: not a retry
    assert api.request("DELETE", f"/strategies/{s['id']}", json=other).status_code == 409  # closed already: refused on state
    assert len(rows(Withdrawal)) == 1 and len(fakechain.payouts) == 1


def test_close_needs_the_owners_signature(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    assert api.request("DELETE", f"/strategies/{sid}", json={"destination_wallet_address": DEST}).status_code == 401
    r, _ = close(api, sid, signer=Signer())
    assert r.status_code == 403 and "not the strategy's owner" in r.json()["error"]
    body = {"destination_wallet_address": str(Keypair().pubkey()), "authorization": auth_close(sid)[1]}  # signed for another address
    assert api.request("DELETE", f"/strategies/{sid}", json=body).status_code == 403
    assert get(api, sid)["status"] == "active" and rows(Withdrawal) == [] and fakechain.payouts == []
    assert close(api, create(api)["id"])[0].status_code == 409  # a pending strategy cannot be closed
    assert api.request("DELETE", "/strategies/nope", json={"destination_wallet_address": DEST}).status_code == 404
    bad = api.request("DELETE", f"/strategies/{sid}", json={"destination_wallet_address": "nope", "authorization": auth_close(sid)[1]})
    assert bad.status_code == 400 and "valid Solana address" in bad.json()["error"]


# ---- close: liquidity ----------------------------------------------------------------

def test_close_fails_clearly_when_the_book_is_empty_and_sends_nothing(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    state.venue.liquidity = D("0.05")  # the book offers 0.05 but closing needs 0.12
    orders = len(state.venue.orders)
    r, body = close(api, sid)
    assert r.status_code == 409 and r.json()["code"] == "NO_LIQUIDITY" and set(r.json()) == {"error", "code"}
    msg = r.json()["error"]
    assert "0.05" in msg and "needs 0.12" in msg and "market maker" in msg and "Nothing was sent" in msg
    a = get(api, sid)
    assert a["status"] == "active" and a["position"]["size_units"] == 0.12 and len(state.venue.orders) == orders
    assert rows(Withdrawal) == [] and fakechain.payouts == []
    with Session(engine) as db:
        assert db.exec(select(UsedNonce)).all() == []  # the signature was not spent: the same request works once liquidity is back
    state.venue.liquidity = None
    again = api.request("DELETE", f"/strategies/{sid}", json=body)
    assert again.status_code == 200 and wd(api, sid, again.json()["id"])["status"] == "completed"


def test_liquidity_that_vanishes_after_the_request_fails_the_withdrawal_and_reopens_the_strategy(api, fakechain):
    s = active(api, fakechain)
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)  # accepted: the book was fine
    state.venue.liquidity = D(0)  # and then it was gone
    assert withdrawals.advance_withdrawal(w.id) == "failed"
    f = wd(api, s["id"], w.id)
    assert f["status"] == "failed" and "NO_LIQUIDITY" in f["failure_reason"] and f["solana_signature"] is None
    assert get(api, s["id"])["status"] == "active" and get(api, s["id"])["position"]["size_units"] == 0.12  # nothing was touched
    assert len(state.venue.orders) == 1 and wrow(w.id).refs.get("needs_operator") is False  # just retry the close later


def test_a_zero_fill_or_price_deviation_during_the_close_fails_it_and_leaves_the_position(api, fakechain):
    s = active(api, fakechain)
    state.venue.fill_fraction = D(0)
    w = close(api, s["id"])[0].json()
    f = wd(api, s["id"], w["id"])
    assert f["status"] == "failed" and "ORDER_NOT_FILLED" in f["failure_reason"] and get(api, s["id"])["status"] == "active"
    state.venue.fill_fraction = D(1)
    set_price(3000)  # far from Pyth
    w = close(api, s["id"])[0].json()
    assert "PRICE_DEVIATION" in wd(api, s["id"], w["id"])["failure_reason"] and get(api, s["id"])["status"] == "active"


def test_a_partial_close_keeps_the_strategy_closing_and_a_second_close_finishes_it(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    state.venue.fill_fraction = D("0.5")
    w1 = close(api, sid)[0].json()
    f1 = wd(api, sid, w1["id"])
    assert f1["status"] == "failed" and "partially closed" in f1["failure_reason"] and f1["solana_signature"] is None
    a = get(api, sid)
    assert a["status"] == "closing" and 0 < a["position"]["size_units"] < 0.12  # part of it is closed, and the ledger knows
    assert a["position"]["size_units"] == float(-state.venue.position(None, M).size) and fakechain.payouts == []
    state.venue.fill_fraction = D(1)
    w2 = close(api, sid)[0].json()  # closing again is allowed: nothing is in flight and no funds left yet
    assert w2["id"] != w1["id"]
    f2 = wd(api, sid, w2["id"])
    assert f2["status"] == "completed" and get(api, sid)["status"] == "closed" and len(fakechain.payouts) == 1
    assert state.venue.position(None, M).size == 0


def test_a_position_the_ledger_cannot_explain_holds_the_close_for_an_operator(api, fakechain):
    s = active(api, fakechain)
    state.venue.set_position("stray", M, D("-0.5"))
    orders = len(state.venue.orders)
    w = close(api, s["id"])[0].json()
    f = wd(api, s["id"], w["id"])
    assert f["status"] == "failed" and "reconciled" in f["failure_reason"] and len(state.venue.orders) == orders
    assert wrow(w["id"]).refs["needs_operator"] is True and api.get("/health").json()["unresolved_withdrawals"] == 1
    assert get(api, s["id"])["status"] == "active"


# ---- release / bridge / payout failures ---------------------------------------------------

def test_release_errors_are_retried_then_succeed(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    calls = []
    real = state.venue.release_margin

    def flaky(market_id, amount):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("venue hiccup")
        return real(market_id, amount)

    monkeypatch.setattr(state.venue, "release_margin", flaky)
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)
    assert [withdrawals.advance_withdrawal(w.id) for _ in range(3)] == ["retry", "retry", "completed"]  # one attempt per tick
    assert len(calls) == 3 and len(fakechain.payouts) == 1 and wrow(w.id).refs["attempts"] == 2


def test_release_that_keeps_failing_ends_failed_for_an_operator_without_paying(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    monkeypatch.setattr(state.venue, "release_margin", lambda *a: (_ for _ in ()).throw(RuntimeError("no master key")))
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)
    results = [withdrawals.advance_withdrawal(w.id) for _ in range(withdrawals.MAX_STEP_ATTEMPTS)]
    assert results[:-1] == ["retry"] * (withdrawals.MAX_STEP_ATTEMPTS - 1) and results[-1] == "failed"
    f = wrow(w.id)
    assert f.status == "failed" and "no master key" in f.failure_reason and f.refs["needs_operator"] and f.refs.get("released_usd") is None
    assert fakechain.payouts == [] and get(api, s["id"])["status"] == "closing"  # the position is closed; the money has not moved
    assert state.venue.position(None, M).size == 0


def test_a_failed_payout_is_never_retried_automatically_and_closing_again_is_refused(api, fakechain):
    s = active(api, fakechain)
    fakechain.payout_fails = True
    w = close(api, s["id"])[0].json()
    f = wd(api, s["id"], w["id"])
    assert f["status"] == "failed" and "result is unknown" in f["failure_reason"] and f["solana_signature"] is None
    assert wrow(w["id"]).refs["released_usd"] is not None  # the money left the venue, so a second close would double pay
    assert withdrawals.advance_pending() == 0 and fakechain.payouts == []  # the tick does not touch a failed withdrawal
    assert get(api, s["id"])["status"] == "closing"
    r, _ = close(api, s["id"])
    assert r.status_code == 409 and "retry-withdrawal" in r.json()["error"] and "twice" in r.json()["error"]
    assert api.get("/health").json()["unresolved_withdrawals"] == 1


def test_the_operator_retries_a_failed_payout_only_after_confirming_it_was_not_sent(api, fakechain):
    s = active(api, fakechain)
    fakechain.payout_fails = True
    w = close(api, s["id"])[0].json()
    fakechain.payout_fails = False
    refuse = runner.invoke(cli_app, ["strategies", "retry-withdrawal", w["id"]])
    assert refuse.exit_code == 1 and "--confirm-not-sent" in " ".join(refuse.output.split())
    assert fakechain.payouts == [] and wd(api, s["id"], w["id"])["status"] == "failed"
    ok = runner.invoke(cli_app, ["strategies", "retry-withdrawal", w["id"], "--confirm-not-sent"])
    assert ok.exit_code == 0, ok.output
    done = wd(api, s["id"], w["id"])
    assert done["status"] == "completed" and done["solana_signature"] == "payout1" and len(fakechain.payouts) == 1
    assert get(api, s["id"])["status"] == "closed" and api.get("/health").json()["unresolved_withdrawals"] == 0
    assert runner.invoke(cli_app, ["strategies", "retry-withdrawal", w["id"]]).exit_code == 1  # no longer failed


def test_a_failure_after_the_payout_never_pays_twice(api, fakechain, monkeypatch):
    """The money went out and its signature is stored; then the attestation step blows up. A retry must only finish."""
    s = active(api, fakechain)
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)
    real = service._record_action
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup after the transfer")
        return real(*args, **kw)

    monkeypatch.setattr(service, "_record_action", flaky)
    assert withdrawals.advance_withdrawal(w.id) == "failed"
    f = wrow(w.id)
    assert f.status == "failed" and f.solana_signature == "payout1" and len(fakechain.payouts) == 1  # sent, and we know it
    assert withdrawals.advance_pending() == 0 and len(fakechain.payouts) == 1  # the tick leaves failed ones alone
    # the operator retries: no --confirm-not-sent needed, because the signature proves it WAS sent; nothing is sent again
    assert withdrawals.retry_withdrawal(w.id) == "completed"
    assert len(fakechain.payouts) == 1 and wrow(w.id).solana_signature == "payout1" and get(api, s["id"])["status"] == "closed"


def test_a_payout_left_unknown_by_a_crash_is_not_resent(api, fakechain):
    s = active(api, fakechain)
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)
    withdrawals._update(w.id, status="bridging", refs={"payout_started": True, "released_usd": "100"})  # crashed mid-payout
    assert withdrawals.advance_withdrawal(w.id) == "failed"
    f = wrow(w.id)
    assert "result is unknown" in f.failure_reason or "earlier attempt" in f.failure_reason
    assert fakechain.payouts == [] and f.refs["needs_operator"] is True


def test_a_restart_resumes_withdrawals_from_where_they_stopped(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    params, a = auth_close(s["id"])
    w = withdrawals.request_close(s["id"], params, a)
    real = withdrawals._payout_step

    def die(wid):
        raise SystemExit("the process was killed mid-withdrawal")  # not an Exception: it escapes the machine, like a real crash

    monkeypatch.setattr(withdrawals, "_payout_step", die)
    with pytest.raises(SystemExit):
        withdrawals.advance_withdrawal(w.id)
    assert wrow(w.id).status == "bridging" and fakechain.payouts == []  # position closed, funds released, payout not made
    assert state.venue.position(None, M).size == 0 and get(api, s["id"])["status"] == "closing"
    monkeypatch.setattr(withdrawals, "_payout_step", real)  # "restart"
    assert withdrawals.advance_pending() == 1  # the watcher tick picks it up
    assert wrow(w.id).status == "completed" and get(api, s["id"])["status"] == "closed" and len(fakechain.payouts) == 1
    assert withdrawals.advance_pending() == 0 and len(fakechain.payouts) == 1  # and it is never done twice


# ---- return excess ----------------------------------------------------------------------

def test_return_excess_keeps_the_hedge_open_and_pays_the_destination(api, fakechain):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    r, _ = excess(api, sid, "50")
    assert r.status_code == 200
    w = r.json()
    assert w["type"] == "return_excess" and w["status"] == "requested" and w["amount_usd"] == 50.0 and isinstance(w["amount_usd"], float)
    f = wd(api, sid, w["id"])
    assert f["status"] == "completed" and f["solana_signature"] == "payout1" and fakechain.payouts[0][:2] == (DEST, D("50"))
    a = get(api, sid)
    assert a["status"] == "active" and a["position"]["size_units"] == 0.12 and a["position"]["margin_usd"] == 250.0  # hedge untouched
    assert state.venue.orders and len(state.venue.orders) == 1  # no trade at all
    (act,) = actions(sid, "return_excess")
    assert act.record["amount_usd"] == "50" and act.signer_public_key == OWNER.pubkey and act.attestation_sig
    assert json.loads(fakechain.memos[-1])["a"] == "return_excess"
    assert api.get("/health").json()["reconciliation"]["ok"] is True  # the venue balance dropped with the ledger


def test_the_excess_cap_keeps_equity_at_1_5x_required_margin(api, fakechain):
    s = active(api, fakechain, amount=300)  # required margin 127.2 -> must keep >= 190.8; equity ~299.86
    sid = s["id"]
    ok, _ = excess(api, sid, "109")
    assert ok.status_code == 200
    # 109 already left: equity ~190.86, so almost nothing more may go
    r, _ = excess(api, sid, "1")
    assert r.status_code == 400 and r.json()["code"] == "WITHDRAW_BELOW_MARGIN" and set(r.json()) == {"error", "code"}
    assert "at most" in r.json()["error"] and "1.5" in r.json()["error"]


def test_the_cap_boundary_is_exact(api, fakechain):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    r, _ = excess(api, sid, "110")  # cap is ~109.06
    assert r.status_code == 400 and "at most 109.0" in r.json()["error"]
    assert excess(api, sid, "109")[0].status_code == 200
    assert get(api, sid)["position"]["margin_usd"] == 191.0


def test_two_excess_requests_cannot_together_exceed_the_cap(api, fakechain):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    params, a = auth_excess(sid, "100")
    w = withdrawals.request_excess(sid, params, a)  # reserved (margin 200), not yet paid
    assert get(api, sid)["position"]["margin_usd"] == 200.0
    r, _ = excess(api, sid, "100")  # the first one's reservation already counts
    assert r.status_code == 400 and r.json()["code"] == "WITHDRAW_BELOW_MARGIN"
    assert withdrawals.advance_withdrawal(w.id) == "completed"


def test_excess_validation_and_signature_rules(api, fakechain):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    zero = excess(api, sid, "0")[0]  # "0" is a well-formed decimal string, but not a positive amount
    assert zero.status_code == 400 and "must be positive" in zero.json()["error"]
    r = api.post(f"/strategies/{sid}/withdrawals", json={"type": "close", "amount_usd": "5", "destination_wallet_address": DEST,
                                                         "authorization": auth_excess(sid, "5")[1]})
    assert r.status_code == 400 and "DELETE" in r.json()["error"]
    assert api.post(f"/strategies/{sid}/withdrawals", json={}).status_code == 400
    p = {"amount_usd": "5", "destination_wallet_address": DEST}
    num = api.post(f"/strategies/{sid}/withdrawals", json={"amount_usd": 5, "destination_wallet_address": DEST,
                                                           "authorization": OWNER.authorization("return_excess", sid, p)})
    assert num.status_code == 403 and "not a JSON number" in num.json()["error"]  # a JSON number is refused before anything is stored
    tampered = api.post(f"/strategies/{sid}/withdrawals", json={"amount_usd": "6", "destination_wallet_address": DEST,
                                                                "authorization": OWNER.authorization("return_excess", sid, p)})
    assert tampered.status_code == 403
    assert excess(api, sid, "5", signer=Signer())[0].status_code == 403
    assert api.post(f"/strategies/{sid}/withdrawals", json={"amount_usd": "5", "destination_wallet_address": DEST}).status_code == 401
    assert excess(api, sid, "5", dest="nope")[0].status_code == 400
    assert excess(api, create(api)["id"], "5")[0].status_code == 409  # pending
    assert rows(Withdrawal) == [] and fakechain.payouts == []
    ok, body = excess(api, sid, "5")
    again = api.post(f"/strategies/{sid}/withdrawals", json=body)  # an identical retry
    assert again.json()["id"] == ok.json()["id"] and len(fakechain.payouts) == 1


def test_a_failed_release_gives_the_reserved_margin_back_and_an_operator_retry_reserves_it_again(api, fakechain, monkeypatch):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    real = state.venue.release_margin
    monkeypatch.setattr(state.venue, "release_margin", lambda *a: (_ for _ in ()).throw(RuntimeError("no master key")))
    params, a = auth_excess(sid, "50")
    w = withdrawals.request_excess(sid, params, a)
    assert get(api, sid)["position"]["margin_usd"] == 250.0  # reserved while it is in flight
    for _ in range(withdrawals.MAX_STEP_ATTEMPTS):
        withdrawals.advance_withdrawal(w.id)
    f = wrow(w.id)
    assert f.status == "failed" and f.refs["needs_operator"] and f.refs["margin_restored"] is True
    assert get(api, sid)["position"]["margin_usd"] == 300.0 and fakechain.payouts == []  # nothing left the venue: given back
    monkeypatch.setattr(state.venue, "release_margin", real)  # the operator fixed the venue
    assert withdrawals.retry_withdrawal(w.id) == "completed"
    assert get(api, sid)["position"]["margin_usd"] == 250.0  # reserved again, exactly once
    assert fakechain.payouts[0][:2] == (DEST, D("50")) and len(fakechain.payouts) == 1


def test_withdrawal_reads_are_org_scoped_and_validated(api, fakechain):
    s = api.post("/strategies", json=BODY, headers={"X-Sereel-Org": "org-a"}).json()
    fund(api, fakechain, s)
    p = {"destination_wallet_address": DEST}
    w = api.request("DELETE", f"/strategies/{s['id']}", json={**p, "authorization": OWNER.authorization("close_strategy", s["id"], p)},
                    headers={"X-Sereel-Org": "org-a"}).json()
    assert [x["id"] for x in api.get(f"/strategies/{s['id']}/withdrawals", headers={"X-Sereel-Org": "org-a"}).json()] == [w["id"]]
    assert api.get(f"/strategies/{s['id']}/withdrawals/{w['id']}", headers={"X-Sereel-Org": "org-b"}).status_code == 404
    assert api.get(f"/strategies/{s['id']}/withdrawals/nope").status_code == 404
    other = create(api, registered_sender_address=str(Keypair().pubkey()))
    assert api.get(f"/strategies/{other['id']}/withdrawals/{w['id']}").status_code == 404  # another strategy's withdrawal
    assert api.get(f"/strategies/{s['id']}/withdrawals/{w['id']}", headers={"X-Sereel-Key": "bad"}).status_code == 401


def test_a_closed_strategy_is_inert_afterwards(api, fakechain):
    s = active(api, fakechain)
    close(api, s["id"])
    sid = s["id"]
    assert close(api, sid)[0].status_code == 409  # already closed
    assert excess(api, sid, "1")[0].status_code == 409
    from test_step1 import patch, rebalance
    assert patch(api, sid, {"hedge_ratio_bps": "3000"})[0].status_code == 409 and rebalance(api, sid)[0].status_code == 409
    assert api.get("/health").json()["active_strategies"] == 0
    assert service.snapshot_all() == 0  # a closed strategy is not snapshotted any more
