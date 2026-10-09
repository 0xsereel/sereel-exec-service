"""Addendum v4.1: update_exposure. The owner signs it and calls POST /strategies/{id}/exposure; the chat can only DRAFT it, from what the manager said,
with every number computed by the server."""
import json
from decimal import Decimal

import pytest
from sqlmodel import Session, select

from app.ai import actions
from app.db import engine
from app.models import Action, Strategy, UsedNonce
from app.state import state
from app.x402 import feed
from auth_helpers import Signer
from test_chat import Script, ctx, say, setup  # noqa: F401  (setup: the chat tests' autouse fixture that switches the agent on)
from test_delegation import DELEGATE, grant, grant_params
from test_step1 import M, OWNER, SENDER, active, get, patch, rebalance, set_price
from test_strategies import create, fund

D = Decimal


def nonces():
    with Session(engine) as db:
        return len(db.exec(select(UsedNonce)).all())


def exposure(api, sid, value="0.07", signer=OWNER, **kw):
    params = {"exposure_oz": value}
    return api.post(f"/strategies/{sid}/exposure", json={**params, "authorization": signer.authorization("update_exposure", sid, params, **kw)})


def half_oz(api, fakechain):
    """A strategy with 0.05 oz of fund exposure, hedged 100% (held 0.05 oz): the 0.05 -> 0.07 example from the addendum."""
    s = create(api, target_exposure_units=0.05, hedge_ratio_bps=10000, expected_amount_usd=54)
    fund(api, fakechain, s, amount=80)  # the 26 USD over is credited as margin, enough to grow the short to 0.07 oz later
    assert get(api, s["id"])["status"] == "active"
    return s["id"]


# ---- the endpoint ------------------------------------------------------------------------------------------------------------------------

def test_the_owner_updates_the_exposure_and_nothing_trades(api, fakechain):
    sid = half_oz(api, fakechain)
    orders = len(state.venue.orders)
    r = exposure(api, sid, "0.07")
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["target_exposure_units"] == 0.07 and row["target_hedge_size_units"] == 0.07 and row["position"]["size_units"] == 0.05  # the target moved, the position did not
    assert row["hedge_gap_units"] == pytest.approx(0.02) and len(state.venue.orders) == orders  # no order was sent
    with Session(engine) as db:
        a = db.exec(select(Action).where(Action.action == "update_exposure")).one()
    assert a.record["event"] == "update_exposure" and a.record["signed_by"] == OWNER.pubkey and a.attestation_sig
    assert (a.record["from"]["target_exposure_units"], a.record["to"]["target_exposure_units"]) == ("0.05", "0.07")
    assert json.loads(fakechain.memos[-1])["a"] == "update_exposure"
    assert get(api, sid)["hedge_ratio_bps"] == 10000  # only the exposure changed


def test_update_exposure_is_validated_and_a_refusal_keeps_the_signature(api, fakechain):
    sid = half_oz(api, fakechain)
    used = nonces()
    assert exposure(api, sid, "0").json()["error"] == "exposure_oz must be positive"
    assert api.post(f"/strategies/{sid}/exposure", json={"authorization": OWNER.authorization("update_exposure", sid, {})}).json()["code"] == "BAD_REQUEST"
    tiny = exposure(api, sid, "0.0461")  # target 0.0461 vs holding 0.05: an 8% gap that would be a $10.34 trade, under the venue minimum
    assert tiny.status_code == 400 and "below the venue's $10 minimum order" in tiny.json()["error"] and "oz" in tiny.json()["error"]
    assert nonces() == used and get(api, sid)["target_exposure_units"] == 0.05  # nothing stored, the signed request is still valid
    inside = exposure(api, sid, "0.051")  # inside the 5% band: nothing would trade, so the minimum does not apply
    assert inside.status_code == 200


def test_numbers_and_the_wrong_signer_are_refused(api, fakechain):
    sid = half_oz(api, fakechain)
    params = {"exposure_oz": "0.07"}
    r = api.post(f"/strategies/{sid}/exposure", json={"exposure_oz": 0.07, "authorization": OWNER.authorization("update_exposure", sid, params)})
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"
    assert api.post(f"/strategies/{sid}/exposure", json=params).json()["code"] == "AUTHORIZATION_REQUIRED"
    assert exposure(api, sid, signer=Signer()).json()["code"] == "AUTHORIZATION_INVALID"
    assert exposure(api, "nope").status_code == 404
    assert get(api, sid)["target_exposure_units"] == 0.05


def test_a_delegate_cannot_update_the_exposure_and_a_closed_strategy_cannot_be_updated(api, fakechain):
    sid = half_oz(api, fakechain)
    grant(api, sid, grant_params(rebalance_within_band_only="false"))
    r = exposure(api, sid, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_NOT_ALLOWED"
    with Session(engine) as db:
        st = db.get(Strategy, sid)
        st.status = "closing"
        db.add(st)
        db.commit()
    assert exposure(api, sid).json()["code"] == "CONFLICT"


def test_an_exposure_update_does_not_show_in_the_sold_hedge_summary_until_it_has_traded(api, fakechain):
    sid = half_oz(api, fakechain)
    sold = lambda: feed.build(sid, ["hedge_summary"])["hedge_summary"]  # noqa: E731
    assert sold() == {"hedge_ratio_pct": "100.00", "hedged_share_of_exposure_pct": "100.00"}
    assert exposure(api, sid, "0.07").status_code == 200
    assert sold() == {"hedge_ratio_pct": "100.00", "hedged_share_of_exposure_pct": "100.00"}  # the pending change is not for sale
    assert rebalance(api, sid)[0].status_code == 200
    assert sold() == {"hedge_ratio_pct": "100.00", "hedged_share_of_exposure_pct": "100.00"}  # held 0.07 of 0.07 now
    assert "update_exposure" not in feed.EXECUTED_ACTIONS  # and its attestation is never sold either


# ---- the chat: it only extracts, the server decides ------------------------------------------------------------------------------------

def strategy_chat(api, sid, message, tool_args, reply="Here is the draft.", session=None):
    s = Script(("tools", [("propose_exposure_update", tool_args)]), ("text", reply))
    r = say(api, s, message, session, ctx(strategy_id=sid))
    return r, s


def test_set_produces_the_server_computed_total_and_a_complete_draft(api, fakechain):
    sid = active(api, fakechain)["id"]  # 0.2 oz of exposure at 60%: holding 0.12
    r, s = strategy_chat(api, sid, "set my exposure to 0.25 oz", {"mode": "set", "value_oz": "0.25"})
    d = r["action_draft"]
    assert r["status"] == "ready" and s.tool_results[-1]["ok"] is True
    assert set(d) == {"type", "params", "summary", "previous_exposure_oz", "new_target_size", "gap_pct"}
    assert d["type"] == "update_exposure" and d["params"] == {"exposure_oz": "0.25"} and d["previous_exposure_oz"] == "0.2"
    assert d["new_target_size"] == "0.15" and d["gap_pct"] == "20.00"  # |0.15 - 0.12| / 0.15
    assert d["summary"] == "Update fund exposure from 0.2 oz to 0.25 oz (target 0.15 oz). No trade; the agent proposes a rebalance on its next check."
    assert r["draft"] is None  # an action draft, not a strategy draft


def test_bought_more_from_0_05_gives_0_07_computed_by_the_server(api, fakechain):
    sid = half_oz(api, fakechain)
    r, s = strategy_chat(api, sid, "I bought 0.02 more", {"mode": "change", "value_oz": "0.02"})
    d = r["action_draft"]
    assert d["params"] == {"exposure_oz": "0.07"} and d["previous_exposure_oz"] == "0.05" and d["new_target_size"] == "0.07" and d["gap_pct"] == "28.57"
    assert d["summary"] == "Update fund exposure from 0.05 oz to 0.07 oz (target 0.07 oz). No trade; the agent proposes a rebalance on its next check."


def test_sold_is_a_negative_change_and_set_and_change_agree(api, fakechain):
    sid = half_oz(api, fakechain)
    sold, _ = strategy_chat(api, sid, "I sold 0.01", {"mode": "change", "value_oz": "-0.01"})
    assert sold["action_draft"]["params"] == {"exposure_oz": "0.04"} and sold["action_draft"]["gap_pct"] == "25.00"
    up_set, _ = strategy_chat(api, sid, "set it to 0.07", {"mode": "set", "value_oz": "0.07"})
    up_chg, _ = strategy_chat(api, sid, "bought 0.02 more", {"mode": "change", "value_oz": "0.02"})
    assert up_set["action_draft"]["params"] == up_chg["action_draft"]["params"] == {"exposure_oz": "0.07"}
    plus, _ = strategy_chat(api, sid, "added +0.02 oz", {"mode": "change", "value_oz": "+0.02"})
    assert plus["action_draft"]["params"] == {"exposure_oz": "0.07"}


def test_the_summary_says_the_agent_rebalances_only_when_it_has_autopilot(api, fakechain):
    from app.ai import agent_key
    from app import solana_client as sol
    from app.config import settings
    sid = half_oz(api, fakechain)
    sol.load_keypair(settings.agent_keypair, create=True)
    grant(api, sid, grant_params(pub=agent_key.pubkey()))
    r, _ = strategy_chat(api, sid, "bought 0.02 more", {"mode": "change", "value_oz": "0.02"})
    assert r["action_draft"]["summary"].endswith("No trade; the agent rebalances on its next check.")


def test_a_negative_or_zero_result_is_refused_with_no_draft(api, fakechain):
    sid = half_oz(api, fakechain)
    r, s = strategy_chat(api, sid, "I sold 0.1", {"mode": "change", "value_oz": "-0.1"}, reply="That can't be right: you only have 0.05 oz.")
    res = s.tool_results[-1]
    assert r["action_draft"] is None and r["status"] == "collecting" and res["ok"] is False and "-0.05 oz" in res["message"] and "close the strategy" in res["message"]
    zero, s2 = strategy_chat(api, sid, "I sold 0.05", {"mode": "change", "value_oz": "-0.05"})
    assert zero["action_draft"] is None and "0 oz" in s2.tool_results[-1]["message"]
    nothing, s3 = strategy_chat(api, sid, "set it to 0.05", {"mode": "set", "value_oz": "0.05"})
    assert nothing["action_draft"] is None and "already 0.05 oz" in s3.tool_results[-1]["message"]
    zero_set, s4 = strategy_chat(api, sid, "set it to 0", {"mode": "set", "value_oz": "0"})
    assert zero_set["action_draft"] is None and "above zero" in s4.tool_results[-1]["message"]


@pytest.mark.parametrize("message,args", [
    ("I bought some more", {"mode": "change", "value_oz": "0.02"}),  # no number said: the model's 0.02 is not the manager's
    ("update my exposure", {"mode": "set", "value_oz": "0.07"}),
    ("0.07 oz", {"mode": "set", "value_oz": "0.07"}),  # a bare number: total or change?
    ("0.07 oz", {"mode": "change", "value_oz": "0.07"}),
    ("bought 0.02 more", {"mode": "set", "value_oz": "0.02"}),  # an addition mislabelled as a total
    ("I sold 0.01 and bought 0.02", {"mode": "change", "value_oz": "0.01"}),  # both directions: unclear
    ("sold 0.01", {"mode": "change", "value_oz": "0.01"}),  # the sign contradicts the words
    ("bought 0.02 more", {"mode": "change", "value_oz": "-0.02"}),
    ("bought 0.03 more", {"mode": "change", "value_oz": "0.02"}),  # the model's number is not the manager's
    ("set it to 0.06", {"mode": "set", "value_oz": "0.07"}),
    ("set it to 0.07", {"mode": "total", "value_oz": "0.07"}),  # not a mode
    ("set it to lots", {"mode": "set", "value_oz": "lots"}),
    ("bought 0.00 more", {"mode": "change", "value_oz": "0"}),
])
def test_anything_ambiguous_or_not_the_managers_own_number_is_a_question_never_a_draft(api, fakechain, message, args):
    sid = half_oz(api, fakechain)
    r, s = strategy_chat(api, sid, message, args, reply="Could you tell me the number and whether it is your new total or a change?")
    res = s.tool_results[-1]
    assert r["action_draft"] is None and r["status"] == "collecting" and res["ok"] is False and res["ask_the_user"], res
    assert get(api, sid)["target_exposure_units"] == 0.05


def test_a_draft_contains_no_number_the_model_produced(api, fakechain):
    sid = half_oz(api, fakechain)
    s = Script(("tools", [("propose_exposure_update", {"mode": "change", "value_oz": "0.02"})]), ("text", "Your new total is 99 oz, and the target is 12 oz, gap 80%."))
    r = say(api, s, "bought 0.02 more", None, ctx(strategy_id=sid))
    d = r["action_draft"]
    blob = json.dumps(d)
    assert "99" not in blob and "12 oz" not in blob and "80" not in blob  # what the model WROTE never reaches the draft
    st = D("0.05") + D("0.02")
    assert d["params"]["exposure_oz"] == str(st).rstrip("0") and D(d["new_target_size"]) == st and D(d["gap_pct"]) == (D("0.02") / st * 100).quantize(D("0.01"))
    assert "99" in r["reply"]  # the reply is the model's text, untouched here; the draft is what is signed and it came from the server


def test_an_update_the_venue_could_not_trade_is_refused_with_the_same_message_as_the_endpoint(api, fakechain):
    sid = half_oz(api, fakechain)
    r, s = strategy_chat(api, sid, "set my exposure to 0.0461", {"mode": "set", "value_oz": "0.0461"})
    res = s.tool_results[-1]
    assert r["action_draft"] is None and res["ok"] is False and "below the venue's $10 minimum order" in res["message"]
    assert res["message"] == exposure(api, sid, "0.0461").json()["error"]  # exactly what signing it would have returned


def test_only_an_active_strategy_can_get_a_draft(api, fakechain):
    sid = half_oz(api, fakechain)
    with Session(engine) as db:
        st = db.get(Strategy, sid)
        st.status = "closing"
        db.add(st)
        db.commit()
    r, s = strategy_chat(api, sid, "set it to 0.07", {"mode": "set", "value_oz": "0.07"})
    assert r["action_draft"] is None and "only be updated while the strategy is active" in s.tool_results[-1]["message"]


def test_the_draft_is_approved_through_the_endpoint_and_that_is_the_only_path(api, fakechain):
    sid = half_oz(api, fakechain)
    r, _ = strategy_chat(api, sid, "bought 0.02 more", {"mode": "change", "value_oz": "0.02"})
    d = r["action_draft"]
    done = exposure(api, sid, d["params"]["exposure_oz"])  # Cantina signs update_exposure {exposure_oz} and calls POST /strategies/{id}/exposure
    assert done.status_code == 200 and done.json()["target_exposure_units"] == 0.07 and done.json()["hedge_gap_units"] == pytest.approx(0.02)
    assert D(d["gap_pct"]) == D(str(done.json()["hedge_gap_bps"])) / 100 and d["new_target_size"] == "0.07"  # the draft's display numbers match what the update then showed
    assert actions.draft(get(api, sid) and actions.strategy_view(sid), "update_exposure")[0] is None  # the generic draft tool cannot make this type


def test_the_tool_exists_only_when_discussing_a_strategy_and_the_model_is_told_to_extract_and_ask(api, fakechain):
    from app.ai import chat
    assert "propose_exposure_update" in [t["function"]["name"] for t in chat.STRATEGY_TOOLS]
    assert "propose_exposure_update" not in [t["function"]["name"] for t in chat.TOOLS]
    sid = half_oz(api, fakechain)
    s = Script(("text", "hi"))
    say(api, s, "hello", None, ctx(strategy_id=sid))
    prompt = s.seen[0][0]["content"]
    assert 'propose_exposure_update' in prompt and "never guess" in prompt and "You never add or subtract" in prompt
    desc = [t for t in chat.STRATEGY_TOOLS if t["function"]["name"] == "propose_exposure_update"][0]["function"]["description"]
    assert "You never compute the new total" in desc and "ask the manager" in desc
    setup = Script(("tools", [("propose_exposure_update", {"mode": "set", "value_oz": "0.07"})]), ("text", "ok"))
    say(api, setup, "set it to 0.07")  # not discussing a strategy: the tool does not exist
    assert setup.tool_results[-1]["ok"] is False and "unknown tool" in setup.tool_results[-1]["error"]


def test_each_chat_mode_runs_only_its_own_tools(api, fakechain):
    """A model that invents a tool, or calls one from the other mode, gets nothing run (a strategy tool with no strategy used to be reachable)."""
    s = Script(("tools", [("get_strategy_status", {}), ("draft_action", {"type": "top_up"}), ("propose_exposure_update", {"mode": "set", "value_oz": "1"})]),
               ("text", "I can't do that here."))
    r = say(api, s, "set 1")  # the setup chat: no strategy
    assert all(x["ok"] is False and "unknown tool" in x["error"] for x in s.tool_log) and len(s.tool_log) == 3 and r["action_draft"] is None
    sid = half_oz(api, fakechain)
    t = Script(("tools", [("set_slot", {"name": "market", "value": M}), ("get_recommendation", {}), ("declare_unsupported", {"reason": "x"})]), ("text", "No."))
    r2 = say(api, t, "hello", None, ctx(strategy_id=sid))
    assert all(x["ok"] is False and "unknown tool" in x["error"] for x in t.tool_log) and r2["status"] == "collecting"  # and the strategy chat cannot edit setup slots
