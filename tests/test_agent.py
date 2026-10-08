"""Step 3: decision rules, the explainer, the monitoring cycle, the feed and proposal resolution. No network, no keys."""
import json
from datetime import timedelta
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select

from app import pyth
from app.ai import actions, decisions, extras, jev, llm, loop
from app.ai import hl_readonly as hl
from app.ai.decide import Decision, decide
from app.ai.explain import explain, template, validate
from app.ai.signals import Signals
from app.ai.state import Snapshot
from app.config import settings
from app.db import engine
from app.models import Action, AgentDecision, now
from auth_helpers import Signer
from test_ai_signals import fake_info
from test_step1 import M, OWNER, SENDER, active, get, patch, rebalance, set_price
from test_step2 import DEST, excess, wd
from app.strategies import withdrawals

D = Decimal
OWN = "OwnerPubkey1111111111111111111111111111111111"


def snap(bps="22.0"):
    return Snapshot(M, "2026-10-08T09:00:00Z", "mainnet", "testnet", False, has_strategy=True,
                    sections={"execution": {"testnet_mark_vs_pyth_bps": bps}, "strategy": {"maintenance_ratio": "1.80", "gap_oz": "0.0300"}})


def view(**over):
    v = {"status": "active", "owner_pubkey": OWN, "leverage": 3, "required_margin_usd": 100.0, "target_hedge_size_units": 0.12,
         "hedge_gap_units": 0.03, "value_usd": 90.0,
         "position": {"margin_usd": 150.0, "maintenance_margin_usd": 50.0, "mark_price_usd": 2650.0, "size_units": 0.09}}
    v.update(over)
    return v


def P(**kw):
    base = {"needs_top_up_soon": "0.05", "should_rebalance": "0.05", "abnormal_price_move": "0.05", "venue_price_divergence": "0.05",
            "liquidity_sufficient": "0.9", "excess_margin_safe_to_return": "0.05", "high_impact_event_soon": "0.02"}
    base.update(kw)
    return {k: D(v) for k, v in base.items()}


RICH = view(value_usd=300.0, position={"margin_usd": 300.0, "maintenance_margin_usd": 30.0, "mark_price_usd": 2650.0, "size_units": 0.12},
            hedge_gap_units=0.0)


# ---- decision rules ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question,v,prob,kind,atype,params", [
    ("needs_top_up_soon", view(), "0.49", "none", None, None),
    ("needs_top_up_soon", view(), "0.50", "suggest", "top_up", {"amount_usd": "10"}),  # equity 90, 2x maintenance 100: +10
    ("needs_top_up_soon", view(), "0.79", "suggest", "top_up", {"amount_usd": "10"}),
    ("needs_top_up_soon", view(), "0.80", "propose", "top_up", {"amount_usd": "10"}),
    ("should_rebalance", view(), "0.49", "none", None, None),
    ("should_rebalance", view(), "0.50", "suggest", "rebalance", {"target_size": "0.12"}),
    ("should_rebalance", view(), "0.80", "propose", "rebalance", {"target_size": "0.12"}),
    ("excess_margin_safe_to_return", RICH, "0.49", "none", None, None),
    ("excess_margin_safe_to_return", RICH, "0.55", "suggest", "return_excess", {"amount_usd": "150"}),  # 300 - 1.5 x 100
    ("excess_margin_safe_to_return", RICH, "0.95", "propose", "return_excess", {"amount_usd": "150"}),
])
def test_each_rule_below_between_and_above_the_thresholds(question, v, prob, kind, atype, params):
    d = decide(snap(), v, P(**{question: prob}))
    assert d.kind == kind and (d.action["type"] if d.action else None) == atype and (d.action["params"] if d.action else None) == params
    assert all(isinstance(x, str) for x in (d.action or {}).get("params", {}).values())  # parameters are strings, computed here


def test_hold_beats_every_other_rule_and_between_thresholds_it_is_a_suggestion():
    d = decide(snap(), view(), P(needs_top_up_soon="0.99", should_rebalance="0.99", venue_price_divergence="0.85"))
    assert d.kind == "hold" and d.action == {"type": "hold", "params": {}} and "venue_price_divergence = 0.85" in d.reason
    d = decide(snap(), view(), P(needs_top_up_soon="0.99", abnormal_price_move="0.80"))
    assert d.kind == "hold"  # exactly at the act threshold
    d = decide(snap(), view(), P(needs_top_up_soon="0.99", abnormal_price_move="0.60"))
    assert d.kind == "suggest" and d.action["type"] == "hold"  # between: advice only, and still wins over the top-up
    assert decide(snap(), view(), P(needs_top_up_soon="0.99", abnormal_price_move="0.49")).action["type"] == "top_up"


def test_precedence_top_up_then_rebalance_then_return_excess():
    both = view(value_usd=90.0)
    d = decide(snap(), both, P(needs_top_up_soon="0.9", should_rebalance="0.9"))
    assert d.action["type"] == "top_up"
    d = decide(snap(), view(value_usd=250.0, position={**view()["position"], "margin_usd": 250.0}), P(should_rebalance="0.9", excess_margin_safe_to_return="0.9"))
    assert d.action["type"] == "rebalance"


def test_a_rebalance_also_needs_liquidity_and_a_trade_the_venue_accepts():
    assert decide(snap(), view(), P(should_rebalance="0.9", liquidity_sufficient="0.49")).kind == "none"
    assert decide(snap(), view(), P(should_rebalance="0.9", liquidity_sufficient="0.50")).kind == "propose"
    tiny = view(hedge_gap_units=0.002)  # $5.30: under the venue minimum
    d = decide(snap(), tiny, P(should_rebalance="0.95"))
    assert d.kind == "none"
    poor = view(position={**view()["position"], "margin_usd": 50.0})  # growing to 0.12 needs 106 of margin
    assert decide(snap(), poor, P(should_rebalance="0.95")).kind == "none"


def test_no_action_when_there_is_nothing_to_do_or_the_numbers_do_not_support_it():
    assert decide(snap(), view(value_usd=200.0), P(needs_top_up_soon="0.99")).kind == "none"  # ratio 4: no top-up amount exists
    assert decide(snap(), view(hedge_gap_units=0.0), P(should_rebalance="0.99")).kind == "none"  # already at target
    assert decide(snap(), view(value_usd=150.0), P(excess_margin_safe_to_return="0.99")).kind == "none"  # equity 1.5x, not > 2x required
    assert decide(snap(), view(), P()).kind == "none"
    # plenty of margin posted but losses have eaten the equity (not above 2x the requirement): nothing to return
    drained = view(value_usd=150.0, position={"margin_usd": 300.0, "maintenance_margin_usd": 30.0, "mark_price_usd": 2650.0, "size_units": 0.12}, hedge_gap_units=0.0)
    assert actions.return_excess_amount(drained) == "150" and decide(snap(), drained, P(excess_margin_safe_to_return="0.99")).kind == "none"


@pytest.mark.parametrize("over", [{"status": "closing"}, {"status": "failed"}, {"status": "pending_funding"}, {"status": "closed"},
                                  {"owner_pubkey": None, "owner_multisig": None}])
def test_hard_caps_closing_failed_or_ownerless_strategies_get_nothing(over):
    d = decide(snap(), view(**over), P(needs_top_up_soon="0.99", should_rebalance="0.99", venue_price_divergence="0.99"))
    assert d.kind == "none" and d.action is None
    assert decide(snap(), view(owner_pubkey=None, owner_multisig="Multisig1111111111111111111111111111111111"), P(needs_top_up_soon="0.99")).kind == "propose"


# ---- execute, and its downgrades ---------------------------------------------------------------------------------------------------

def test_only_a_delegated_rebalance_can_execute_never_a_money_movement():
    t0 = now()
    assert decide(snap(), view(), P(should_rebalance="0.9"), delegated=True, now=t0).kind == "execute"
    assert decide(snap(), view(), P(should_rebalance="0.9"), delegated=False, now=t0).kind == "propose"
    assert decide(snap(), view(), P(needs_top_up_soon="0.99"), delegated=True, now=t0).kind == "propose"
    assert decide(snap(), RICH, P(excess_margin_safe_to_return="0.99"), delegated=True, now=t0).kind == "propose"
    assert decide(snap(), view(), P(should_rebalance="0.6"), delegated=True, now=t0).kind == "suggest"  # not act-level: never executes


def test_execute_is_downgraded_to_propose_when_testnet_is_over_50_bps_from_pyth():
    t0 = now()
    d = decide(snap("50.0"), view(), P(should_rebalance="0.9"), delegated=True, now=t0)
    assert d.kind == "execute" and d.downgraded_from is None  # 50 is allowed: the rule is "above 50"
    d = decide(snap("50.1"), view(), P(should_rebalance="0.9"), delegated=True, now=t0)
    assert d.kind == "propose" and d.downgraded_from == "execute" and d.action["type"] == "rebalance"
    assert "50.1 bps from Pyth (limit 50)" in d.reason
    d = decide(snap("-75.0"), view(), P(should_rebalance="0.9"), delegated=True, now=t0)
    assert d.kind == "propose" and "75.0 bps" in d.reason  # the sign does not matter
    d = decide(snap("60"), view(), P(needs_top_up_soon="0.99"), delegated=True, now=t0)
    assert d.kind == "propose" and d.downgraded_from is None  # a top-up was never going to execute: nothing was downgraded


def test_at_most_one_execution_per_ten_minutes_per_strategy():
    t0 = now()
    d = decide(snap(), view(), P(should_rebalance="0.9"), delegated=True, now=t0, last_executed_at=t0 - timedelta(minutes=9, seconds=59))
    assert d.kind == "propose" and d.downgraded_from == "execute" and "last 10 minutes" in d.reason
    d = decide(snap(), view(), P(should_rebalance="0.9"), delegated=True, now=t0, last_executed_at=t0 - timedelta(minutes=10, seconds=1))
    assert d.kind == "execute"


# ---- explanation ---------------------------------------------------------------------------------------------------------------------

GOOD = {"headline": "Top-up proposed", "explanation": "Margin is thin; a top-up helps.", "risk_note": ""}


def fake_llm(monkeypatch, content, calls=None):
    def f(messages, tools=None, json_mode=False):
        if calls is not None:
            calls.append((messages, json_mode))
        if isinstance(content, Exception):
            raise content
        return {"role": "assistant", "content": content if isinstance(content, str) else json.dumps(content)}
    monkeypatch.setattr(llm, "chat_completion", f)


def top_up_decision():
    return decide(snap(), view(), P(needs_top_up_soon="0.9"))


def test_a_valid_model_explanation_is_used_in_json_mode_from_structured_facts_only(monkeypatch):
    calls = []
    fake_llm(monkeypatch, GOOD, calls)
    out = explain(top_up_decision(), view(), snap(), P(needs_top_up_soon="0.9"))
    assert out == GOOD and calls[0][1] is True
    facts = json.loads(calls[0][0][1]["content"])
    assert set(facts) == {"decision", "action", "rule", "downgraded_from", "probabilities", "strategy", "market", "template_text"}
    assert facts["action"] == {"type": "top_up", "params": {"amount_usd": "10"}} and facts["decision"] == "propose"


@pytest.mark.parametrize("bad", [
    {"headline": "x" * 81, "explanation": "ok", "risk_note": ""}, {"headline": "ok", "explanation": "x" * 601, "risk_note": ""},
    {"headline": "ok", "explanation": "ok", "risk_note": "x" * 201}, {"headline": "ok", "explanation": "ok"},
    {"headline": "ok", "explanation": "ok", "risk_note": "", "extra": "x"}, {"headline": "", "explanation": "ok", "risk_note": ""},
    {"headline": 5, "explanation": "ok", "risk_note": ""}, ["not", "an", "object"], "not json at all", "", "null",
])
def test_an_explanation_that_fails_the_schema_falls_back_to_the_template(monkeypatch, bad):
    fake_llm(monkeypatch, bad)
    d = top_up_decision()
    assert explain(d, view(), snap(), P()) == template(d, view(), snap())
    assert validate(bad if isinstance(bad, dict) else None) is None or bad == GOOD


def test_a_model_failure_or_a_heartbeat_uses_the_template_and_a_heartbeat_never_calls_the_model(monkeypatch):
    fake_llm(monkeypatch, llm.LLMUnavailable("HTTP 500"))
    d = top_up_decision()
    out = explain(d, view(), snap(), P())
    assert out["headline"] == "Proposed: top up $10" and "1.80" in out["explanation"] and out["risk_note"]
    calls = []
    fake_llm(monkeypatch, GOOD, calls)
    none = explain(Decision("none", reason="no rule fired"), view(), snap(), P())
    assert none["headline"] == "No action needed" and calls == []


def test_the_templates_state_the_decision_and_any_downgrade():
    d = decide(snap("80"), view(), P(should_rebalance="0.9"), delegated=True, now=now())
    t = template(d, view(), snap("80"))
    assert t["headline"] == "Proposed: rebalance to 0.12 oz" and "80.0 bps from Pyth" in t["risk_note"]
    assert template(Decision("hold", {"type": "hold", "params": {}}, "venue_price_divergence = 0.90: do not trade this cycle"), view(), snap())["headline"] == "Hold: do not trade now"


# ---- storing, superseding, the heartbeat ----------------------------------------------------------------------------------------------

def put(sid, decision="propose", atype="rebalance", params=None, **kw):
    return decisions.store(sid, state_hash="h", signals_source="jev", signals_network="mainnet", question_set_version="v", signals={"a": "0.10"},
                           decision=decision, action=None if atype is None else {"type": atype, "params": params or {}},
                           explanation={"headline": "h", "explanation": "e", "risk_note": ""}, reason=kw.get("reason", "r"))


def rows_for(sid):
    with Session(engine) as db:
        return db.exec(select(AgentDecision).where(AgentDecision.strategy_id == sid).order_by(AgentDecision.at, AgentDecision.id)).all()


def test_heartbeats_update_in_place_and_do_not_flood_the_feed():
    a = put("s1", "none", None)
    b = put("s1", "none", None)
    assert a.id == b.id and len(rows_for("s1")) == 1
    c = put("s1", "propose", "top_up", {"amount_usd": "5"})
    d = put("s1", "none", None)
    assert d.id != c.id and len(rows_for("s1")) == 3  # the first heartbeat, the proposal, and a NEW heartbeat after it (the proposal keeps its place)
    assert put("s1", "none", None).id == d.id and len(rows_for("s1")) == 3


def test_a_newer_proposal_of_the_same_type_dismisses_the_older_one_other_types_stay():
    r1 = put("s1", "propose", "rebalance", {"target_size": "0.1"})
    t1 = put("s1", "suggest", "top_up", {"amount_usd": "5"})
    r2 = put("s1", "propose", "rebalance", {"target_size": "0.12"})
    by = {r.id: r for r in rows_for("s1")}
    assert by[r1.id].outcome == "dismissed" and by[r2.id].outcome == "pending" and by[t1.id].outcome == "pending"
    assert decisions.has_unread_proposal("s1")
    decisions.dismiss("s1", r2.id)
    decisions.dismiss("s1", t1.id)
    assert not decisions.has_unread_proposal("s1")
    put("s2", "hold", "hold")
    assert decisions.has_unread_proposal("s2") is False  # `hold` is a notice, not a proposal: nothing for the owner to approve


def test_agent_mode_follows_the_switch(monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", False)
    assert decisions.agent_mode("s") is None
    monkeypatch.setattr(settings, "agent_enabled", True)
    assert decisions.agent_mode("s") == "monitoring"


# ---- the cycle, end to end (fake network, real strategy) ------------------------------------------------------------------------------------

@pytest.fixture
def cycle(api, fakechain, monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(hl, "info", fake_info())
    monkeypatch.setattr(extras, "pyth_history", lambda feed, now_s=None: ({"1h": D(2640), "24h": D(2600), "7d": D(2500)}, None))
    from types import SimpleNamespace
    from app.ai import state as ai_state

    # the snapshot sees a mainnet-like Pyth price; the venue keeps its own (so strategies still activate at 2650)
    monkeypatch.setattr(ai_state, "pyth", SimpleNamespace(get_price=lambda *a, **k: pyth.PythPrice(D("4126.3"), 1791448800, "f", conf=D("0.16"))))
    calls = {"jev": 0, "llm": 0}
    probs = {}

    def ask(state, names, client=None):
        calls["jev"] += 1
        return jev.JevResult({n: D(probs.get(n, "0.05")) for n in names}, "jev-1.13.0", 5, "Authorization: Bearer")

    monkeypatch.setattr(jev, "ask", ask)
    fake_llm(monkeypatch, GOOD)
    real = llm.chat_completion
    monkeypatch.setattr(llm, "chat_completion", lambda *a, **k: calls.__setitem__("llm", calls["llm"] + 1) or real(*a, **k))
    set_price(2650)
    loop._last_run_once.clear()
    return calls, probs


def weaken(monkeypatch, ratio="1.60"):
    """Make the strategy's equity 1.6x its maintenance margin (the simulated venue's tiny maintenance rate cannot get there by price alone)."""
    real = actions.strategy_view

    def weak(sid):
        v = real(sid)
        v["value_usd"] = float(D(str(v["position"]["maintenance_margin_usd"])) * D(ratio))
        return v

    monkeypatch.setattr(actions, "strategy_view", weak)


def run_once_req(api, sid, signer=OWNER, **kw):
    params = {"strategy_id": sid}
    return api.post("/agent/run-once", json={"strategy_id": sid, "authorization": signer.authorization("run_once", sid, params, **kw)})


def test_run_once_on_a_healthy_strategy_returns_a_complete_none_decision_without_a_model_call(api, fakechain, cycle):
    calls, _ = cycle
    s = active(api, fakechain)
    r = run_once_req(api, s["id"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["decision"] == "none" and d["action"] is None and d["outcome"] == "pending" and d["strategy_id"] == s["id"]
    assert set(d) == {"id", "at", "strategy_id", "state_hash", "signals_source", "signals_network", "question_set_version", "signals", "decision",
                      "action", "reason", "downgraded_from", "explanation", "outcome", "action_id", "attestation_sig"}
    assert d["signals_source"] == "jev" and d["signals_network"] == "mainnet" and len(d["state_hash"]) == 64
    assert all(isinstance(v, str) for v in d["signals"].values()) and d["signals"]["needs_top_up_soon"] == "0.05"
    assert d["explanation"]["headline"] == "No action needed" and calls["llm"] == 0 and calls["jev"] == 1


def test_a_weak_strategy_gets_a_top_up_proposal_whose_amount_is_computed_not_written(api, fakechain, cycle, monkeypatch):
    calls, probs = cycle
    s = active(api, fakechain)
    weaken(monkeypatch)
    probs.update(needs_top_up_soon="0.93")
    fake_llm(monkeypatch, {"headline": "Top up $9999 now", "explanation": "Please send $9999 immediately.", "risk_note": ""})
    d = run_once_req(api, s["id"]).json()
    expected = actions.top_up_amount(actions.strategy_view(s["id"]))
    assert d["decision"] == "propose" and d["action"] == {"type": "top_up", "params": {"amount_usd": expected}} and expected != "9999"
    assert "9999" in d["explanation"]["headline"]  # stored as written, never parsed
    row = get(api, s["id"])
    assert row["has_unread_proposal"] is True and row["agent_mode"] == "monitoring" and row["fund_id"] == "fund-1"
    assert [x["id"] for x in api.get(f"/strategies/{s['id']}/agent/decisions").json()] == [d["id"]]


def test_a_bad_explanation_falls_back_to_the_template_and_the_decision_is_untouched(api, fakechain, cycle, monkeypatch):
    calls, probs = cycle
    s = active(api, fakechain)
    weaken(monkeypatch)
    probs.update(needs_top_up_soon="0.93")
    fake_llm(monkeypatch, {"headline": "x" * 500, "explanation": "e", "risk_note": ""})
    d = run_once_req(api, s["id"]).json()
    assert d["decision"] == "propose" and d["explanation"]["headline"].startswith("Proposed: top up $")


def test_run_once_has_a_30_second_cooldown_that_returns_the_latest_decision_and_calls_nothing(api, fakechain, cycle):
    calls, _ = cycle
    s = active(api, fakechain)
    first = run_once_req(api, s["id"]).json()
    second = run_once_req(api, s["id"])
    assert second.status_code == 200 and second.json()["id"] == first["id"] and calls["jev"] == 1  # no second Jev call
    loop._last_run_once[s["id"]] -= settings.agent_run_once_cooldown_s + 1
    run_once_req(api, s["id"])
    assert calls["jev"] == 2


def test_run_once_needs_the_owners_signature_an_active_strategy_and_an_enabled_agent(api, fakechain, cycle, monkeypatch):
    s = active(api, fakechain)
    r = api.post("/agent/run-once", json={"strategy_id": s["id"]})
    assert r.status_code == 401 and r.json()["code"] == "AUTHORIZATION_REQUIRED"
    r = run_once_req(api, s["id"], signer=Signer())
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"
    assert run_once_req(api, "nope").json()["code"] == "NOT_FOUND"
    assert api.post("/agent/run-once", json={}).json()["code"] == "BAD_REQUEST"
    monkeypatch.setattr(settings, "agent_enabled", False)
    assert run_once_req(api, s["id"]).json()["code"] == "AGENT_DISABLED"
    monkeypatch.setattr(settings, "agent_enabled", True)
    assert api.post("/agent/run-once", json={"strategy_id": s["id"], "authorization": OWNER.authorization("run_once", s["id"], {"strategy_id": s["id"]}),
                                             "x": 1}).status_code == 200


def test_signals_unavailable_when_neither_pyth_nor_hyperliquid_can_be_read(api, fakechain, cycle, monkeypatch):
    s = active(api, fakechain)
    from types import SimpleNamespace
    from app.ai import state as ai_state

    monkeypatch.setattr(ai_state, "pyth", SimpleNamespace(get_price=lambda *a, **k: (_ for _ in ()).throw(pyth.PriceError("STALE_PRICE", "down", 503))))
    monkeypatch.setattr(hl, "info", lambda *a, **k: (_ for _ in ()).throw(hl.SignalsReadError("down")))
    r = run_once_req(api, s["id"])
    assert r.status_code == 503 and r.json()["code"] == "SIGNALS_UNAVAILABLE" and set(r.json()) == {"error", "code"}
    assert decisions.latest(s["id"]) is None  # nothing was invented


def test_jev_down_means_rules_and_the_decision_says_so(api, fakechain, cycle, monkeypatch):
    s = active(api, fakechain)
    monkeypatch.setattr(jev, "ask", lambda *a, **k: (_ for _ in ()).throw(jev.JevError("down")))
    d = run_once_req(api, s["id"]).json()
    assert d["signals_source"] == "rules" and d["decision"] == "none"


def test_one_strategys_failure_does_not_stop_the_others(api, fakechain, cycle, monkeypatch):
    a, b = active(api, fakechain), active(api, fakechain)
    real = actions.strategy_view
    monkeypatch.setattr(actions, "strategy_view", lambda sid: (_ for _ in ()).throw(RuntimeError("boom")) if sid == a["id"] else real(sid))
    assert loop.run_cycle() == 1
    assert decisions.latest(a["id"]) is None and decisions.latest(b["id"]) is not None and loop.last_cycle["at"] is not None


def test_the_cycle_registers_only_when_the_agent_is_enabled(monkeypatch):
    class Sched:
        jobs = []

        def add_job(self, fn, *a, **k):
            self.jobs.append(k["id"])

    monkeypatch.setattr(settings, "agent_enabled", False)
    s = Sched()
    loop.register(s)
    assert s.jobs == []
    monkeypatch.setattr(settings, "agent_enabled", True)
    loop.register(s)
    assert s.jobs == ["agent-cycle"]


# ---- dismissing -------------------------------------------------------------------------------------------------------------------------

def dismiss_req(api, sid, did, signer=OWNER, **kw):
    params = {"decision_id": did}
    return api.post(f"/strategies/{sid}/agent/decisions/{did}/dismiss", json={"authorization": signer.authorization("dismiss_decision", sid, params, **kw)})


def test_the_owner_can_dismiss_a_proposal_and_nobody_else(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    p = put(sid, "propose", "top_up", {"amount_usd": "5"})
    assert get(api, sid)["has_unread_proposal"] is True
    r = api.post(f"/strategies/{sid}/agent/decisions/{p.id}/dismiss", json={})
    assert r.status_code == 401 and r.json()["code"] == "AUTHORIZATION_REQUIRED"
    assert dismiss_req(api, sid, p.id, signer=Signer()).status_code == 403
    assert get(api, sid)["has_unread_proposal"] is True
    r = dismiss_req(api, sid, p.id)
    assert r.status_code == 200 and r.json()["outcome"] == "dismissed" and r.json()["id"] == p.id
    assert get(api, sid)["has_unread_proposal"] is False


def test_dismiss_checks_the_decision_before_spending_a_nonce_and_never_changes_a_resolved_one(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    from app.models import UsedNonce
    with Session(engine) as db:
        before = len(db.exec(select(UsedNonce)).all())
    assert dismiss_req(api, sid, "no-such-decision").status_code == 404
    other = active(api, fakechain)
    q = put(other["id"], "propose", "top_up", {"amount_usd": "5"})
    assert dismiss_req(api, sid, q.id).status_code == 404  # another strategy's decision does not exist here
    with Session(engine) as db:
        assert len(db.exec(select(UsedNonce)).all()) == before
    p = put(sid, "propose", "rebalance", {"target_size": "0.1"})
    with Session(engine) as db:
        row = db.get(AgentDecision, p.id)
        row.outcome = "executed"
        db.add(row)
        db.commit()
    assert dismiss_req(api, sid, p.id).json()["outcome"] == "executed"  # an answered proposal stays answered
    q2 = put(sid, "propose", "top_up", {"amount_usd": "5"})
    bad = api.post(f"/strategies/{sid}/agent/decisions/{q2.id}/dismiss",
                   json={"decision_id": "other", "authorization": OWNER.authorization("dismiss_decision", sid, {"decision_id": q2.id})})
    assert bad.status_code == 400 and bad.json()["code"] == "BAD_REQUEST"


# ---- proposals resolve when the owner acts ----------------------------------------------------------------------------------------------

def last_action(sid, name):
    with Session(engine) as db:
        return db.exec(select(Action).where(Action.strategy_id == sid, Action.action == name).order_by(Action.created_at.desc(), Action.id)).first()


def outcome(did):
    with Session(engine) as db:
        d = db.get(AgentDecision, did)
        return d.outcome, d.action_id


def test_approving_then_executing_a_rebalance_resolves_the_proposal_and_links_the_action(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    patch(api, sid, {"target_exposure_units": "0.1"})  # target 0.06: a real trade
    p = put(sid, "propose", "rebalance", {"target_size": "0.06"})
    t = put(sid, "propose", "top_up", {"amount_usd": "5"})
    assert get(api, sid)["has_unread_proposal"] is True
    assert rebalance(api, sid)[0].status_code == 200
    act = last_action(sid, "rebalance")
    assert outcome(p.id) == ("executed", act.id) and outcome(t.id) == ("pending", None)  # only the same type is answered
    assert get(api, sid)["has_unread_proposal"] is True  # the top-up is still waiting


def test_a_rebalance_that_traded_nothing_does_not_answer_a_proposal(api, fakechain):
    s = active(api, fakechain)
    p = put(s["id"], "propose", "rebalance", {"target_size": "0.12"})
    assert rebalance(api, s["id"])[0].status_code == 200  # already at target: nothing traded
    assert outcome(p.id) == ("pending", None)


def test_a_top_up_proposal_is_answered_only_when_the_top_up_funding_completes(api, fakechain):
    from test_strategies import fund
    s = active(api, fakechain)
    sid = s["id"]
    p = put(sid, "propose", "top_up", {"amount_usd": "50"})
    d = api.post(f"/strategies/{sid}/deposits", json={"amount_usd": 50, "registered_sender_address": SENDER}).json()
    assert outcome(p.id) == ("pending", None)  # the intent exists but nothing arrived
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=20)
    assert outcome(p.id) == ("pending", None)  # underfunded: still not complete
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=30)
    assert api.get(f"/strategies/{sid}/deposits/{d['id']}").json()["status"] == "confirmed"
    assert outcome(p.id) == ("executed", last_action(sid, "deposit").id)
    assert get(api, sid)["has_unread_proposal"] is False


def test_returning_excess_margin_resolves_a_return_excess_proposal_when_the_withdrawal_completes(api, fakechain):
    s = active(api, fakechain, amount=300)
    sid = s["id"]
    p = put(sid, "propose", "return_excess", {"amount_usd": "100"})
    r, _ = excess(api, sid, "100")
    assert r.status_code == 200, r.text
    wid = r.json()["id"]
    assert wd(api, sid, wid)["status"] == "completed"  # the test client runs the background payout before returning
    assert outcome(p.id) == ("executed", last_action(sid, "return_excess").id)


def test_resolution_never_breaks_the_owners_action(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    monkeypatch.setattr(decisions, "Session", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup")))
    assert decisions.resolve_executed(s["id"], "rebalance") == 0  # swallowed
    monkeypatch.undo()


# ---- status and the contract ----------------------------------------------------------------------------------------------------------------

def test_status_has_every_field(api, monkeypatch):
    monkeypatch.setattr(settings, "agent_enabled", True)
    monkeypatch.setattr(settings, "jev_api_key", "")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    r = api.get("/agent/status")
    assert r.status_code == 200
    s = r.json()
    assert set(s) == {"enabled", "signals_network", "execution_network", "interval_s", "last_cycle_at", "jev_model", "llm_model", "agent_pubkey"}
    assert s["enabled"] is True and s["signals_network"] == "mainnet" and s["execution_network"] == "testnet" and isinstance(s["interval_s"], int)
    assert s["jev_model"] is None and s["llm_model"] == settings.llm_model and s["agent_pubkey"] is None
    assert api.get("/agent/status", headers={"X-Sereel-Key": "x"}).status_code == 401


def test_decisions_are_listed_newest_first_with_a_limit(api, fakechain):
    s = active(api, fakechain)
    ids = [put(s["id"], "propose", t, {"amount_usd": "5"}).id for t in ("top_up", "return_excess")]
    got = api.get(f"/strategies/{s['id']}/agent/decisions").json()
    assert [d["id"] for d in got] == ids[::-1]
    assert len(api.get(f"/strategies/{s['id']}/agent/decisions?limit=1").json()) == 1
    assert api.get("/strategies/nope/agent/decisions").status_code == 404


def test_strategy_rows_carry_the_agent_fields_always(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    row = get(api, s["id"])
    assert row["agent_mode"] == "monitoring" or row["agent_mode"] is None
    assert isinstance(row["has_unread_proposal"], bool) and "fund_id" in row
    monkeypatch.setattr(settings, "agent_enabled", False)
    assert get(api, s["id"])["agent_mode"] is None
    pending = api.post("/strategies", json={**__import__("test_strategies").BODY}).json()
    assert pending["has_unread_proposal"] is False and "agent_mode" in pending
