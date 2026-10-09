"""Step 4: delegation and the autonomous rebalance. A delegate can sign `rebalance` and nothing else, inside limits the owner signed."""
import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from app import auth, delegates, pyth
from app import solana_client as sol
from app.ai import actions, agent_key, decisions, extras, jev, llm, loop
from app.ai import hl_readonly as hl
from app.ai import state as ai_state
from app.config import settings
from app.db import engine
from app.models import Action, AgentDecision, Delegate, UsedNonce, now
from app.state import state
from app.strategies import attest as att
from auth_helpers import Signer
from cli.sereel_cli import app as cli_app
from test_agent import GOOD, fake_llm, weaken  # noqa: F401
from test_ai_signals import fake_info
from test_step1 import M, OWNER, SENDER, active, get, patch, rebalance, set_price
from test_step2 import close, excess

D = Decimal
DELEGATE = Signer()
runner = CliRunner()


def iso_in(**kw):
    return (now() + timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def grant_params(pub=None, **over):
    p = {"delegate_pubkey": pub or DELEGATE.pubkey, "allowed_actions": "rebalance", "max_rebalance_oz_per_day": "0.5",
         "rebalance_within_band_only": "true", "expires_at": iso_in(days=7)}
    p.update(over)
    return p


def grant(api, sid, params=None, signer=OWNER, **kw):
    params = params or grant_params()
    return api.post(f"/strategies/{sid}/delegates", json={**params, "authorization": signer.authorization("grant_delegate", sid, params, **kw)})


def revoke(api, sid, pub=None, signer=OWNER):
    params = {"delegate_pubkey": pub or DELEGATE.pubkey}
    return api.request("DELETE", f"/strategies/{sid}/delegates/{params['delegate_pubkey']}",
                       json={"authorization": signer.authorization("revoke_delegate", sid, params)})


def nonces():
    with Session(engine) as db:
        return len(db.exec(select(UsedNonce)).all())


def traded_target(api, fakechain, exposure="0.1"):
    """An active strategy whose target is 0.06 while it holds 0.12: a 0.06 oz rebalance, outside the 5% band."""
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": exposure})
    return s["id"]


# ---- granting ---------------------------------------------------------------------------------------------------------------------------

def test_the_owner_grants_a_delegate_and_the_limits_echo_back_exactly_as_signed(api, fakechain):
    s = active(api, fakechain)
    p = grant_params(max_rebalance_oz_per_day="0.50", rebalance_within_band_only="false")
    r = grant(api, s["id"], p)
    assert r.status_code == 200, r.text
    g = r.json()
    assert (g["delegate_pubkey"], g["allowed_actions"], g["max_rebalance_oz_per_day"], g["rebalance_within_band_only"]) == (DELEGATE.pubkey, "rebalance", "0.50", "false")
    assert g["status"] == "active" and g["expires_at"] == p["expires_at"] and g["granted_at"].endswith("Z") and g["attestation_sig"]
    assert isinstance(g["rebalance_within_band_only"], str)  # the one exception to JSON booleans: it echoes the signed grant
    listed = api.get(f"/strategies/{s['id']}/delegates").json()
    assert [x["delegate_pubkey"] for x in listed] == [DELEGATE.pubkey] and listed[0]["max_rebalance_oz_per_day"] == "0.50"
    for field in ("delegate_pubkey", "allowed_actions", "max_rebalance_oz_per_day", "rebalance_within_band_only", "expires_at", "granted_at", "status", "attestation_sig"):
        assert field in listed[0]
    with Session(engine) as db:
        a = db.exec(select(Action).where(Action.action == "grant_delegate")).one()
    memo = json.loads(fakechain.memos[-1])
    assert a.record["signed_by"] == OWNER.pubkey and a.record["delegate_pubkey"] == DELEGATE.pubkey and memo["a"] == "grant_delegate" and memo["h"] == att.record_hash(a.record)


@pytest.mark.parametrize("over,fragment", [
    ({"delegate_pubkey": "nope"}, "valid Solana address"), ({"allowed_actions": "rebalance,close"}, "exactly \"rebalance\""),
    ({"allowed_actions": "withdraw"}, "exactly \"rebalance\""), ({"max_rebalance_oz_per_day": "0"}, "must be positive"),
    ({"rebalance_within_band_only": "yes"}, "\"true\" or \"false\""), ({"rebalance_within_band_only": "TRUE"}, "\"true\" or \"false\""),
    ({"expires_at": "2020-01-01T00:00:00Z"}, "in the future"), ({"expires_at": "2026-10-15"}, "ISO 8601 UTC"),
    ({"expires_at": "2026-10-15T09:00:00+07:00"}, "ISO 8601 UTC"), ({"expires_at": "2026-13-45T99:99:99Z"}, "not a real date"),
])
def test_a_bad_grant_is_refused_before_the_owners_signature_is_spent(api, fakechain, over, fragment):
    s = active(api, fakechain)
    used = nonces()
    r = grant(api, s["id"], grant_params(**over))
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and set(r.json()) == {"error", "code"} and fragment in r.json()["error"]
    assert nonces() == used and api.get(f"/strategies/{s['id']}/delegates").json() == []


def test_the_expiry_must_be_in_the_future_and_at_most_30_days_out(api, fakechain):
    s = active(api, fakechain)
    assert grant(api, s["id"], grant_params(expires_at=iso_in(days=30))).status_code == 200  # exactly 30 days is allowed
    over = grant(api, s["id"], grant_params(expires_at=iso_in(days=30, minutes=5)))
    assert over.status_code == 400 and "at most 30 days" in over.json()["error"]
    assert grant(api, s["id"], grant_params(expires_at=iso_in(seconds=-1))).status_code == 400


def test_numbers_and_non_flat_values_are_refused_in_a_grant(api, fakechain):
    s = active(api, fakechain)
    p = grant_params()
    body = {**{**p, "max_rebalance_oz_per_day": 0.5}, "authorization": OWNER.authorization("grant_delegate", s["id"], p)}
    r = api.post(f"/strategies/{s['id']}/delegates", json=body)
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"
    body = {**{**p, "rebalance_within_band_only": True}, "authorization": OWNER.authorization("grant_delegate", s["id"], p)}
    assert api.post(f"/strategies/{s['id']}/delegates", json=body).json()["code"] == "AUTHORIZATION_INVALID"


def test_the_owner_cannot_grant_itself_and_only_an_active_strategy_takes_a_grant(api, fakechain):
    s = active(api, fakechain)
    assert "does not need" in grant(api, s["id"], grant_params(pub=OWNER.pubkey)).json()["error"]
    from test_strategies import create
    pending = create(api)
    r = grant(api, pending["id"])
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


def test_granting_needs_the_owners_signature_not_a_strangers_not_a_delegates(api, fakechain):
    s = active(api, fakechain)
    assert api.post(f"/strategies/{s['id']}/delegates", json=grant_params()).json()["code"] == "AUTHORIZATION_REQUIRED"
    stranger = grant(api, s["id"], signer=Signer())
    assert stranger.status_code == 403 and stranger.json()["code"] == "AUTHORIZATION_INVALID"
    assert grant(api, s["id"]).status_code == 200
    by_delegate = grant(api, s["id"], grant_params(pub=Signer().pubkey), signer=DELEGATE)  # a delegate cannot widen its own power
    assert by_delegate.status_code == 403 and by_delegate.json()["code"] == "DELEGATE_NOT_ALLOWED"
    assert len(api.get(f"/strategies/{s['id']}/delegates").json()) == 1


def test_regranting_replaces_the_active_grant_and_keeps_history(api, fakechain):
    s = active(api, fakechain)
    grant(api, s["id"], grant_params(max_rebalance_oz_per_day="0.1"))
    grant(api, s["id"], grant_params(max_rebalance_oz_per_day="0.9"))
    rows = api.get(f"/strategies/{s['id']}/delegates").json()
    assert sorted((r["status"], r["max_rebalance_oz_per_day"]) for r in rows) == [("active", "0.9"), ("revoked", "0.1")]
    assert delegates.active_grant(s["id"], DELEGATE.pubkey).max_rebalance_oz_per_day == "0.9"


def test_revoking_takes_effect_at_once_is_signed_and_attested(api, fakechain):
    sid = traded_target(api, fakechain)
    grant(api, sid, grant_params(rebalance_within_band_only="false"))
    assert revoke(api, sid, signer=Signer()).status_code == 403
    assert api.request("DELETE", f"/strategies/{sid}/delegates/{DELEGATE.pubkey}", json={}).json()["code"] == "AUTHORIZATION_REQUIRED"
    r = revoke(api, sid)
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["revoke_attestation_sig"]
    again = rebalance(api, sid, signer=DELEGATE)[0]
    assert again.status_code == 403 and again.json()["code"] == "DELEGATE_NOT_ALLOWED" and "revoked" in again.json()["error"]
    assert get(api, sid)["position"]["size_units"] == 0.12  # nothing traded
    assert revoke(api, sid).status_code == 404  # no active grant left
    assert revoke(api, "nope").status_code == 404
    regrant = grant(api, sid, grant_params(rebalance_within_band_only="false"))
    assert regrant.status_code == 200 and rebalance(api, sid, signer=DELEGATE)[0].status_code == 200  # the owner can grant it again


# ---- what a delegate may and may not do ----------------------------------------------------------------------------------------------------

def test_a_delegate_can_rebalance_and_it_is_recorded_and_attested_as_a_delegates(api, fakechain):
    sid = traded_target(api, fakechain)
    grant(api, sid)
    r, _ = rebalance(api, sid, signer=DELEGATE)
    assert r.status_code == 200 and r.json()["position"]["size_units"] == pytest.approx(0.06)
    with Session(engine) as db:
        a = db.exec(select(Action).where(Action.action == "rebalance").order_by(Action.created_at.desc())).first()
    assert a.signer_public_key == DELEGATE.pubkey and a.record["signed_by"] == DELEGATE.pubkey and a.record["signed_by_role"] == "delegate"
    assert a.record["delegate_grant"] == {"max_rebalance_oz_per_day": "0.5", "expires_at": delegates.iso(delegates.latest(sid, DELEGATE.pubkey).expires_at),
                                         "rebalance_within_band_only": "true"}
    memo = json.loads(fakechain.memos[-1])
    assert memo["a"] == "rebalance" and memo["by"] == "delegate" and memo["h"] == att.record_hash(a.record)


@pytest.mark.parametrize("what", ["return_excess", "close", "edit", "change_owner", "grant", "revoke", "dismiss", "run_once"])
def test_a_delegate_cannot_do_anything_but_rebalance(api, fakechain, what):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    grant(api, sid, grant_params(rebalance_within_band_only="false"))
    before = get(api, sid)
    used = nonces()
    if what == "return_excess":
        r, _ = excess(api, sid, "10", signer=DELEGATE)
    elif what == "close":
        r, _ = close(api, sid, signer=DELEGATE)
    elif what == "edit":
        r, _ = patch(api, sid, {"hedge_ratio_bps": "5000"}, signer=DELEGATE)
    elif what == "change_owner":
        params = {"owner_pubkey": DELEGATE.pubkey}
        r = api.post(f"/strategies/{sid}/owner", json={**params, "authorization": DELEGATE.authorization("change_owner", sid, params)})
    elif what == "grant":
        r = grant(api, sid, grant_params(pub=Signer().pubkey), signer=DELEGATE)
    elif what == "revoke":
        r = revoke(api, sid, signer=DELEGATE)
    elif what == "dismiss":
        d = decisions.store(sid, state_hash="h", signals_source="jev", signals_network="mainnet", question_set_version="v", signals={}, decision="propose",
                            action={"type": "top_up", "params": {"amount_usd": "5"}}, explanation={}, reason="r")
        r = api.post(f"/strategies/{sid}/agent/decisions/{d.id}/dismiss",
                     json={"authorization": DELEGATE.authorization("dismiss_decision", sid, {"decision_id": d.id})})
    else:
        settings.agent_enabled = True
        try:
            r = api.post("/agent/run-once", json={"strategy_id": sid, "authorization": DELEGATE.authorization("run_once", sid, {"strategy_id": sid})})
        finally:
            settings.agent_enabled = False
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_NOT_ALLOWED", (what, r.text)
    assert "may only sign `rebalance`" in r.json()["error"]
    after = get(api, sid)
    assert {k: after[k] for k in ("owner_pubkey", "hedge_ratio_bps", "status", "position")} == {k: before[k] for k in ("owner_pubkey", "hedge_ratio_bps", "status", "position")}


def test_a_key_with_no_grant_is_not_a_delegate_it_is_just_not_the_owner(api, fakechain):
    sid = traded_target(api, fakechain)
    r, _ = rebalance(api, sid, signer=Signer())
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"  # unchanged behaviour for strangers
    grant(api, sid)
    other = Signer()
    assert rebalance(api, sid, signer=other)[0].json()["code"] == "AUTHORIZATION_INVALID"  # a grant to someone else gives this key nothing


def test_a_grant_on_one_strategy_gives_nothing_on_another(api, fakechain):
    a, b = traded_target(api, fakechain), traded_target(api, fakechain)
    grant(api, a, grant_params(rebalance_within_band_only="false"))
    r, _ = rebalance(api, b, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"


def test_an_expired_grant_is_refused_and_listed_as_expired(api, fakechain):
    sid = traded_target(api, fakechain)
    grant(api, sid)
    with Session(engine) as db:
        g = db.exec(select(Delegate)).one()
        g.expires_at = now() - timedelta(seconds=1)
        db.add(g)
        db.commit()
    r, _ = rebalance(api, sid, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_NOT_ALLOWED" and "expired" in r.json()["error"]
    assert api.get(f"/strategies/{sid}/delegates").json()[0]["status"] == "expired"
    assert get(api, sid)["position"]["size_units"] == 0.12


def test_a_delegates_authorization_cannot_be_replayed(api, fakechain):
    sid = traded_target(api, fakechain)
    grant(api, sid)
    params = {}
    a = DELEGATE.authorization("rebalance", sid, params)
    assert api.post(f"/strategies/{sid}/rebalance", json={"authorization": a}).status_code == 200
    again = api.post(f"/strategies/{sid}/rebalance", json={"authorization": a})
    assert again.status_code == 403 and "replay" in again.json()["error"]


# ---- the limits ----------------------------------------------------------------------------------------------------------------------------

def test_the_daily_size_limit_is_enforced_by_the_server(api, fakechain):
    sid = traded_target(api, fakechain)  # a 0.06 oz rebalance is due
    grant(api, sid, grant_params(max_rebalance_oz_per_day="0.05"))
    r, _ = rebalance(api, sid, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_LIMIT_EXCEEDED" and "0.06 oz" in r.json()["error"] and "0.05 oz per day" in r.json()["error"]
    assert get(api, sid)["position"]["size_units"] == 0.12 and get(api, sid)["status"] == "active"  # nothing traded, still active
    grant(api, sid, grant_params(max_rebalance_oz_per_day="0.06"))  # exactly the size is allowed
    assert rebalance(api, sid, signer=DELEGATE)[0].status_code == 200


def test_the_limit_counts_everything_the_delegate_moved_in_24_hours_and_forgets_older(api, fakechain):
    sid = traded_target(api, fakechain)
    grant(api, sid, grant_params(max_rebalance_oz_per_day="0.07"))
    assert rebalance(api, sid, signer=DELEGATE)[0].status_code == 200  # moved 0.06
    patch(api, sid, {"target_exposure_units": "0.2"})  # target 0.12 again: another 0.06
    r, _ = rebalance(api, sid, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_LIMIT_EXCEEDED" and "0.06 oz already moved" in r.json()["error"]
    assert delegates.traded_last_24h(sid, DELEGATE.pubkey) == D("0.06")
    with Session(engine) as db:  # the earlier trade is now 25 hours old
        a = db.exec(select(Action).where(Action.action == "rebalance", Action.signer_public_key == DELEGATE.pubkey)).one()
        a.created_at = now() - timedelta(hours=25)
        db.add(a)
        db.commit()
    assert delegates.traded_last_24h(sid, DELEGATE.pubkey) == 0
    assert rebalance(api, sid, signer=DELEGATE)[0].status_code == 200
    # the owner's own rebalances never count against a delegate's limit
    assert Decimal("0.06") == delegates.traded_last_24h(sid, DELEGATE.pubkey)


def test_band_only_forbids_a_forced_rebalance_and_false_allows_it(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    patch(api, sid, {"target_exposure_units": "0.21"})  # target 0.126: a 4.8% gap, inside the 5% band, a viable trade if forced
    grant(api, sid, grant_params(rebalance_within_band_only="true"))
    r, _ = rebalance(api, sid, signer=DELEGATE, force=True)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_LIMIT_EXCEEDED" and "within the band only" in r.json()["error"]
    assert get(api, sid)["position"]["size_units"] == 0.12
    grant(api, sid, grant_params(rebalance_within_band_only="false"))
    r, _ = rebalance(api, sid, signer=DELEGATE, force=True)
    assert r.status_code == 200 and r.json()["position"]["size_units"] == pytest.approx(0.126)


def test_a_delegate_still_gets_every_ordinary_check(api, fakechain):
    sid = traded_target(api, fakechain)
    grant(api, sid)
    state.venue.liquidity = D(0)
    r, _ = rebalance(api, sid, signer=DELEGATE)
    assert r.status_code == 409 and r.json()["code"] == "NO_LIQUIDITY"  # the same book check as for the owner
    state.venue.liquidity = None


# ---- the agent's own key ----------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def agent(api, fakechain, monkeypatch):
    """The agent enabled with its own key, fake market data, and a Jev that returns whatever `probs` says."""
    monkeypatch.setattr(settings, "agent_enabled", True)
    sol.load_keypair(settings.agent_keypair, create=True)
    monkeypatch.setattr(hl, "info", fake_info())
    monkeypatch.setattr(extras, "pyth_history", lambda feed, now_s=None: ({"1h": D(2640), "24h": D(2600), "7d": D(2500)}, None))
    px = {"v": D("4126.3")}
    monkeypatch.setattr(ai_state, "pyth", SimpleNamespace(get_price=lambda *a, **k: pyth.PythPrice(px["v"], 1791448800, "f", conf=D("0.16"))))
    probs = {}
    monkeypatch.setattr(jev, "ask", lambda state_text, names, client=None: jev.JevResult({n: D(probs.get(n, "0.05")) for n in names}, "jev-1.13.0", 5, "Authorization: Bearer"))
    fake_llm(monkeypatch, GOOD)
    loop._last_run_once.clear()
    set_price(2650)
    return probs, px


def grant_agent(api, sid, **over):
    return grant(api, sid, grant_params(pub=agent_key.pubkey(), **over))


def cycle(sid):
    loop._last_run_once.clear()
    return decisions.out(loop.run_once(sid))


def test_the_agent_key_signs_requests_the_server_accepts(api, fakechain, agent):
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    a = agent_key.sign_authorization("rebalance", sid, {})
    assert a["publicKey"] == agent_key.pubkey() and auth.verify(a, "rebalance", sid, {}).signer == agent_key.pubkey()
    with pytest.raises(Exception):  # the same nonce twice is a replay, for the agent like anyone
        auth.verify(a, "rebalance", sid, {})


def test_with_a_grant_a_confident_rebalance_decision_executes_and_is_attested_with_its_evidence(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    assert get(api, sid)["agent_mode"] == "autopilot"
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    d = cycle(sid)
    assert d["decision"] == "execute" and d["outcome"] == "executed" and d["action"] == {"type": "rebalance", "params": {"target_size": "0.06"}}
    assert get(api, sid)["position"]["size_units"] == pytest.approx(0.06)
    with Session(engine) as db:
        act = db.exec(select(Action).where(Action.action == "rebalance").order_by(Action.created_at.desc())).first()
    assert d["action_id"] == act.id and d["attestation_sig"] == act.attestation_sig and act.signer_public_key == agent_key.pubkey()
    assert act.record["signed_by_role"] == "delegate" and act.record["agent"]["decision_id"] == d["id"] and act.record["agent"]["state_hash"] == d["state_hash"]
    assert act.record["agent"]["signals"]["should_rebalance"] == "0.91" and act.record["agent"]["model"] == "jev-1.13.0"
    memo = json.loads(fakechain.memos[-1])
    assert memo["by"] == "delegate" and memo["sh"] == d["state_hash"] and memo["p"] == {"sr": "0.91", "ls": "0.93"}
    assert memo["q"] == d["question_set_version"] and memo["m"] == "jev-1.13.0" and memo["h"] == att.record_hash(act.record)
    assert len(fakechain.memos[-1].encode()) <= att.MAX_MEMO_BYTES


def test_without_a_grant_the_same_decision_is_only_a_proposal(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    d = cycle(sid)
    assert d["decision"] == "propose" and d["outcome"] == "pending" and get(api, sid)["position"]["size_units"] == 0.12
    assert get(api, sid)["agent_mode"] == "monitoring"


def test_a_grant_to_someone_else_does_not_make_the_agent_act(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant(api, sid)  # to DELEGATE, not to the agent's key
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    assert cycle(sid)["decision"] == "propose" and get(api, sid)["agent_mode"] == "monitoring"


def test_money_movements_never_execute_even_with_a_grant(api, fakechain, agent, monkeypatch):
    probs, _ = agent
    s = active(api, fakechain)
    sid = s["id"]
    grant_agent(api, sid)
    weaken(monkeypatch)
    probs.update(needs_top_up_soon="0.97")
    d = cycle(sid)
    assert d["decision"] == "propose" and d["action"]["type"] == "top_up" and d["outcome"] == "pending"


def test_the_grants_limits_turn_an_execute_into_a_proposal_before_anything_is_signed(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid, max_rebalance_oz_per_day="0.05")  # the rebalance is 0.06
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    d = cycle(sid)
    assert d["decision"] == "propose" and d["downgraded_from"] == "execute" and "0.05 oz per day" in d["reason"]
    assert get(api, sid)["position"]["size_units"] == 0.12
    with Session(engine) as db:
        assert db.exec(select(Action).where(Action.signer_public_key == agent_key.pubkey())).first() is None  # nothing was even signed


def test_a_far_off_execution_venue_downgrades_execute_to_propose(api, fakechain, agent):
    probs, px = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    px["v"] = D("4100")  # testnet mark 4135.4 is 86 bps above
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    d = cycle(sid)
    assert d["decision"] == "propose" and d["downgraded_from"] == "execute" and "bps from Pyth (limit 50)" in d["reason"]
    assert get(api, sid)["position"]["size_units"] == 0.12


def test_one_execution_per_ten_minutes(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    assert cycle(sid)["decision"] == "execute"
    patch(api, sid, {"target_exposure_units": "0.2"})  # another rebalance is now due
    d = cycle(sid)
    assert d["decision"] == "propose" and d["downgraded_from"] == "execute" and "last 10 minutes" in d["reason"]


def test_a_failed_execution_is_recorded_as_failed_with_the_reason(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    state.venue.liquidity = D(0)
    try:
        d = cycle(sid)
    finally:
        state.venue.liquidity = None
    assert d["decision"] == "execute" and d["outcome"] == "failed" and "NO_LIQUIDITY" in d["reason"] and d["action_id"] is None
    assert get(api, sid)["position"]["size_units"] == 0.12 and get(api, sid)["status"] == "active"


def test_a_server_side_refusal_is_recorded_as_rejected_not_failed(api, fakechain, agent, monkeypatch):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    monkeypatch.setattr(loop, "_within_the_grant", lambda sid, d, view: d)  # skip the pre-check: the server must still refuse
    with Session(engine) as db:
        g = db.exec(select(Delegate)).one()
        g.max_rebalance_oz_per_day = "0.01"
        db.add(g)
        db.commit()
    d = cycle(sid)
    assert d["outcome"] == "rejected" and "DELEGATE_LIMIT_EXCEEDED" in d["reason"] and get(api, sid)["position"]["size_units"] == 0.12


def test_revoking_or_expiring_the_grant_stops_the_agent_acting(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    revoke(api, sid, pub=agent_key.pubkey())
    assert cycle(sid)["decision"] == "propose" and get(api, sid)["agent_mode"] == "monitoring"
    assert get(api, sid)["position"]["size_units"] == 0.12


def test_no_agent_key_means_no_autopilot(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    grant_agent(api, sid)
    import os
    os.remove(settings.resolve(settings.agent_keypair))
    assert agent_key.pubkey() is None and decisions.has_active_delegate(sid) is False
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    assert cycle(sid)["decision"] == "propose"


def test_an_agent_execution_resolves_the_pending_rebalance_proposal(api, fakechain, agent):
    probs, _ = agent
    sid = traded_target(api, fakechain)
    old = decisions.store(sid, state_hash="h", signals_source="jev", signals_network="mainnet", question_set_version="v", signals={}, decision="propose",
                          action={"type": "rebalance", "params": {"target_size": "0.06"}}, explanation={}, reason="earlier")
    grant_agent(api, sid)
    probs.update(should_rebalance="0.91", liquidity_sufficient="0.93")
    d = cycle(sid)
    assert d["outcome"] == "executed"
    with Session(engine) as db:
        assert db.get(AgentDecision, old.id).outcome == "executed"  # answered by the agent's trade; the owner has nothing left to approve
    assert get(api, sid)["has_unread_proposal"] is False


# ---- the key itself and where its public half shows up -------------------------------------------------------------------------------------------

def test_agent_pubkey_appears_in_health_and_status_once_the_key_exists(api, monkeypatch):
    assert api.get("/health").json()["agent_pubkey"] is None and api.get("/agent/status").json()["agent_pubkey"] is None
    kp = sol.load_keypair(settings.agent_keypair, create=True)
    assert api.get("/health").json()["agent_pubkey"] == str(kp.pubkey()) == api.get("/agent/status").json()["agent_pubkey"]


def test_sereel_agent_key_creates_prints_only_the_public_key_and_never_overwrites():
    r = runner.invoke(cli_app, ["agent", "key"])
    assert r.exit_code == 0 and "created" in r.output
    pub = agent_key.pubkey()
    assert pub in r.output
    secret = json.dumps(list(bytes(sol.agent_kp()))), str(bytes(sol.agent_kp()))
    assert not any(x in r.output for x in secret) and settings.resolve(settings.agent_keypair).stat().st_mode & 0o777 == 0o600
    again = runner.invoke(cli_app, ["agent", "key"])
    assert "kept" in again.output and agent_key.pubkey() == pub


def test_the_agent_key_holds_no_sol_target_and_is_part_of_init_keys():
    from cli import setup
    assert "agent" in setup.KEYS and "agent" not in setup.SOL_TARGETS  # created with the others, never funded


def test_memo_stays_within_the_size_limit_with_the_longest_realistic_extras():
    extra = {"by": "delegate", "sh": "f" * 64, "p": {"sr": "0.91", "ls": "0.93"}, "q": "2026-10-08.2", "m": "jev-1.13.0"}
    memo = att.memo_for("a" * 36, "fund-" + "x" * 59, "rebalance", {"x": 1}, extra)
    assert len(memo.encode()) <= att.MAX_MEMO_BYTES and json.loads(memo)["by"] == "delegate"
