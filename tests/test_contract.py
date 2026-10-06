"""Cantina v4 service contract, field by field (copied from the v4 document), exercised through the real API.

The v4 document defines Stages 4-5. /health and /markets were declared out of scope there ("ask if those contracts are still
needed in writing"), so they are checked against the ORIGINAL product spec instead and flagged as such.
"""
import re
from datetime import datetime

import pytest
from solders.keypair import Keypair

from app import models
from app.state import state
from test_step1 import patch, rebalance, set_price
from test_step2 import DEST, auth_close, close, excess
from test_strategies import BODY, M, OWNER, SENDER, create, fund, get

# ---- v4, verbatim -----------------------------------------------------------------------------------------------------
STRATEGY_FIELDS = ["id", "template", "status", "fund_id", "fund_name", "market_id", "market_symbol", "hedge_ratio_bps", "leverage",
                   "rebalance_band_bps", "target_exposure_units", "target_hedge_size_units", "target_hedge_size_usd",
                   "required_margin_usd", "return_wallet_address", "funding_address", "intent_id", "registered_sender_address",
                   "expected_amount_usd", "received_amount_usd", "shortfall_usd", "expires_at", "owner_user_id", "created_at",
                   "updated_at", "deployed_at", "closed_at", "position"]
POSITION_FIELDS = ["side", "size_units", "entry_price_usd", "mark_price_usd", "margin_usd", "unrealized_pnl_usd", "realized_pnl_usd",
                   "funding_paid_usd", "fees_usd", "margin_health_bps", "maintenance_margin_usd", "liquidation_price_usd", "hl_order_ids"]
DEPOSIT_FIELDS = ["id", "strategy_id", "intent_id", "amount_usd", "expected_amount_usd", "received_amount_usd", "shortfall_usd",
                  "source_wallet_address", "registered_sender_address", "status", "expires_at", "solana_signature", "created_at"]
WITHDRAWAL_FIELDS = ["id", "strategy_id", "type", "amount_usd", "destination_wallet_address", "status", "failure_reason",
                     "solana_signature", "attestation_url", "created_at", "updated_at"]
VALUE_FIELDS = ["margin_usd", "unrealized_pnl_usd", "realized_pnl_usd", "funding_usd", "fees_usd", "value_usd", "as_of"]
STRATEGY_STATUS = {"pending_funding", "active", "rebalancing", "closing", "closed", "expired", "cancelled", "failed"}
DEPOSIT_STATUS = {"pending_funding", "confirmed", "expired", "cancelled"}
WITHDRAWAL_STATUS = {"requested", "position_closed", "released", "bridging", "completed", "failed"}
TIMESTAMP_KEYS = {"created_at", "updated_at", "expires_at", "as_of", "deployed_at", "closed_at"}


def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from walk(v, f"{path}.{k}")
            yield path + "." + k, k, v
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, f"{path}[{i}]")


def check_conventions(obj):
    """Timestamps are ISO 8601 strings, money is a JSON number, never a string or integer cents."""
    for path, key, value in walk(obj):
        if key in TIMESTAMP_KEYS and value is not None:
            assert isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", value), (path, value)
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        if key.endswith("_usd") and value is not None:
            assert isinstance(value, (int, float)) and not isinstance(value, bool), (path, value)  # a number, not "12.5"
        if key.endswith("_bps") and value is not None:
            assert isinstance(value, int) and not isinstance(value, bool), (path, value)


def keys_exact(obj, fields):
    assert set(fields) <= set(obj), f"missing: {sorted(set(fields) - set(obj))}"


# ---- enums ----------------------------------------------------------------------------------------------------------------

def test_status_enums_are_exactly_the_documented_ones():
    mine = lambda prefix: {v for k, v in vars(models).items() if k.startswith(prefix) and isinstance(v, str)}
    assert mine("S_") == STRATEGY_STATUS and mine("D_") == DEPOSIT_STATUS and mine("W_") == WITHDRAWAL_STATUS


# ---- Stage 4: intents and strategies -------------------------------------------------------------------------------------

def test_create_strategy_object_matches_v4(api, fakechain):
    r = api.post("/strategies", json=BODY)
    assert r.status_code == 200  # 200 for every call: no 201
    s = r.json()
    keys_exact(s, STRATEGY_FIELDS)
    assert s["template"] == "delta_neutral_hedge" and s["status"] == "pending_funding" and s["position"] is None
    assert s["received_amount_usd"] is None and s["shortfall_usd"] is None and s["deployed_at"] is None and s["closed_at"] is None
    assert isinstance(s["id"], str) and isinstance(s["intent_id"], str) and s["intent_id"] != s["id"]  # opaque strings
    assert s["funding_address"] == fakechain.funding and isinstance(s["hedge_ratio_bps"], int) and isinstance(s["leverage"], int)
    assert isinstance(s["target_exposure_units"], float) or isinstance(s["target_exposure_units"], int)
    check_conventions(s)
    assert s["status"] in STRATEGY_STATUS


def test_underfunded_strategy_shows_received_and_shortfall(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=100)
    a = get(api, s["id"])
    assert a["status"] == "pending_funding" and a["received_amount_usd"] == 100 and a["shortfall_usd"] == pytest.approx(27.2)
    check_conventions(a)


def test_active_strategy_has_a_live_position_on_get_and_on_every_list_row(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    one = api.get(f"/strategies/{s['id']}").json()
    listed = api.get("/strategies")
    assert listed.status_code == 200 and isinstance(listed.json(), list)  # a bare array, no {data: [...]} envelope
    row = next(x for x in listed.json() if x["id"] == s["id"])
    for obj in (one, row):
        keys_exact(obj, STRATEGY_FIELDS)
        keys_exact(obj["position"], POSITION_FIELDS)  # populated live: no per-strategy follow-up call needed
        assert obj["position"]["side"] == "short" and obj["position"]["size_units"] == 0.12
        assert all(isinstance(x, str) for x in obj["position"]["hl_order_ids"]) and obj["position"]["hl_order_ids"]
        check_conventions(obj)
    assert one["status"] == "active" and one["deployed_at"]


def test_funding_address_shape_and_global_scope(api, fakechain):
    r = api.get("/strategies/funding-address")
    assert r.status_code == 200 and r.json() == {"address": fakechain.funding, "stablecoin_mint": fakechain.mint, "network": "devnet"}


def test_top_up_intent_is_a_v4_strategy_deposit(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    r = api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 25, "registered_sender_address": SENDER})
    assert r.status_code == 200
    d = r.json()
    keys_exact(d, DEPOSIT_FIELDS)
    assert d["status"] == "pending_funding" and d["status"] in DEPOSIT_STATUS and d["amount_usd"] == d["expected_amount_usd"] == 25
    assert d["received_amount_usd"] is None and d["solana_signature"] is None and d["shortfall_usd"] is None
    check_conventions(d)
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=10)
    d2 = api.get(f"/strategies/{s['id']}/deposits/{d['id']}").json()
    assert d2["received_amount_usd"] == 10 and d2["shortfall_usd"] == 15 and d2["status"] == "pending_funding"


def test_cancel_returns_the_updated_strategy_and_409_when_it_is_too_late(api, fakechain):
    s = create(api)
    r = api.post(f"/strategies/{s['id']}/cancel")  # no request body
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    keys_exact(r.json(), STRATEGY_FIELDS)
    assert api.post(f"/strategies/{s['id']}/cancel").status_code == 409
    s2 = create(api, registered_sender_address=str(Keypair().pubkey()))
    fund(api, fakechain, s2, sender=s2["registered_sender_address"])
    assert api.post(f"/strategies/{s2['id']}/cancel").status_code == 409  # after the intent activated


def test_patch_and_rebalance_return_the_updated_strategy(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    r, _ = patch(api, s["id"], {"hedge_ratio_bps": "7000", "target_exposure_units": "0.2"})
    assert r.status_code == 200
    keys_exact(r.json(), STRATEGY_FIELDS)
    assert r.json()["hedge_ratio_bps"] == 7000 and r.json()["target_hedge_size_units"] == pytest.approx(0.14)
    r, _ = rebalance(api, s["id"], force=True)
    assert r.status_code == 200
    keys_exact(r.json(), STRATEGY_FIELDS)
    check_conventions(r.json())


# ---- Stage 5: /value and withdrawals -------------------------------------------------------------------------------------

def test_value_matches_v4_and_the_definitions_it_states(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    set_price(2640)
    v = api.get(f"/strategies/{s['id']}/value").json()
    keys_exact(v, VALUE_FIELDS)
    check_conventions(v)
    assert v["value_usd"] == pytest.approx(v["margin_usd"] + v["unrealized_pnl_usd"])  # literal: margin + unrealized
    hedge = v["unrealized_pnl_usd"] + v["realized_pnl_usd"] + v["funding_usd"] - v["fees_usd"]  # what the frontend computes
    assert v["hedge_pnl_usd"] == pytest.approx(hedge)
    assert v["unrealized_pnl_usd"] != 0 and v["fees_usd"] != 0  # real, independently populated values


def test_return_excess_is_a_v4_withdrawal_and_polls_to_completed(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=300)
    r, _ = excess(api, s["id"], "50")
    assert r.status_code == 200
    w = r.json()
    keys_exact(w, WITHDRAWAL_FIELDS)
    assert w["type"] == "return_excess" and w["status"] == "requested" and w["failure_reason"] is None and w["solana_signature"] is None
    check_conventions(w)
    polled = api.get(f"/strategies/{s['id']}/withdrawals/{w['id']}")  # what the frontend polls every 5 seconds
    assert polled.status_code == 200
    p = polled.json()
    keys_exact(p, WITHDRAWAL_FIELDS)
    assert p["status"] == "completed" and p["status"] in WITHDRAWAL_STATUS and p["solana_signature"] and p["attestation_url"]


def test_close_is_a_withdrawal_of_type_close_not_a_strategy(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    r, _ = close(api, s["id"])  # DELETE with a JSON body
    assert r.status_code == 200
    w = r.json()
    keys_exact(w, WITHDRAWAL_FIELDS)
    assert w["type"] == "close" and "position" not in w and w["status"] in WITHDRAWAL_STATUS  # the deliberate frontend-side correction
    done = api.get(f"/strategies/{s['id']}/withdrawals/{w['id']}").json()
    assert done["status"] == "completed" and done["amount_usd"] > 0  # finalized by completed
    assert get(api, s["id"])["status"] == "closed"


# ---- conventions -------------------------------------------------------------------------------------------------------------

def test_every_non_2xx_is_error_and_code_only(api, fakechain):
    s = create(api)
    for r in (api.get("/strategies/nope"), api.post("/strategies", json={}), api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 5,
              "registered_sender_address": SENDER}), api.get("/strategies/funding-address", headers={"X-Sereel-Key": "bad"})):
        assert r.status_code >= 400
        assert set(r.json()) == {"error", "code"} and isinstance(r.json()["error"], str) and isinstance(r.json()["code"], str), r.json()
    assert api.get("/strategies/nope").status_code == 404 and api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 5,
           "registered_sender_address": SENDER}).status_code == 409  # a pending strategy cannot be topped up
    assert api.get("/strategies/funding-address", headers={"X-Sereel-Key": "bad"}).status_code == 401


def test_every_route_but_health_needs_the_api_key(api):
    for method, path in (("get", "/strategies"), ("get", "/strategies/funding-address"), ("get", "/markets"), ("get", "/payments/mint"),
                         ("get", "/strategies/x"), ("get", "/strategies/x/value"), ("get", "/strategies/x/history"),
                         ("get", "/strategies/x/withdrawals")):
        assert getattr(api, method)(path, headers={"X-Sereel-Key": "wrong"}).status_code == 401, path
    assert api.get("/health", headers={"X-Sereel-Key": "wrong"}).status_code == 200


def test_unknown_or_other_orgs_strategy_is_404(api, fakechain):
    s = api.post("/strategies", json=BODY, headers={"X-Sereel-Org": "org-a"}).json()
    for path in (f"/strategies/{s['id']}", f"/strategies/{s['id']}/value", f"/strategies/{s['id']}/history", f"/strategies/{s['id']}/withdrawals"):
        assert api.get(path, headers={"X-Sereel-Org": "org-b"}).status_code == 404, path


# ---- KNOWN, DELIBERATE DEVIATIONS from the v4 document (decided with the product owner) ---------------------------------------

def test_known_deviations_are_exactly_these(api, fakechain):
    # 1. create needs an owner (owner_pubkey or owner_multisig): the v4 create body has no such field
    no_owner = {k: v for k, v in BODY.items() if k != "owner_pubkey"}
    assert api.post("/strategies", json=no_owner).status_code == 400
    # 2. signed amounts are strings in the body of PATCH and withdrawals (v4 drafted JSON numbers)
    s = create(api)
    fund(api, fakechain, s)
    p = {"hedge_ratio_bps": "7000"}
    num = api.patch(f"/strategies/{s['id']}", json={"hedge_ratio_bps": 7000, "authorization": OWNER.authorization("edit_hedge_settings", s["id"], p)})
    assert num.status_code == 403 and num.json()["code"] == "AUTHORIZATION_INVALID"
    # 3. additive fields exist on the strategy and /value (the frontend ignores unknown keys)
    a = get(api, s["id"])
    assert {"owner_pubkey", "owner_multisig", "market_closed", "failure_reason", "hedge_gap_units", "hedge_gap_bps"} <= set(a)
    assert "hedge_pnl_usd" in api.get(f"/strategies/{s['id']}/value").json()


# ---- /health and /markets: checked against the ORIGINAL spec (v4 left them out of scope) -------------------------------------

def test_health_has_the_fields_the_original_spec_lists(api, fakechain):
    h = api.get("/health").json()
    # "version, venue, Hyperliquid network and account margin, Solana network, funding address, stablecoin mint,
    #  active strategies count, active schedules count"
    for key in ("version", "venue", "funding_address", "stablecoin_mint", "active_strategies", "active_schedules"):
        assert key in h, key
    assert h["hyperliquid"]["network"] in ("testnet", "mainnet") and "margin" in h["hyperliquid"] and h["solana"]["network"] in ("devnet", "other")
    assert isinstance(h["active_strategies"], int) and isinstance(h["active_schedules"], int)
    # additions beyond the spec: leverage assertion, reconciliation, unresolved counters
    assert {"leverage", "mode"} <= set(h["hyperliquid"]) and {"ok", "difference_usd"} <= set(h["reconciliation"])
    assert "unresolved_transfers" in h and "unresolved_withdrawals" in h


def test_markets_lists_markets_with_live_mark_and_pyth_price(api):
    rows = api.get("/markets").json()
    assert isinstance(rows, list) and len(rows) == 1
    m = rows[0]
    assert m["market_id"] == M and m["symbol"] == "XAU" and isinstance(m["mark_price_usd"], float) and isinstance(m["pyth_price_usd"], float)
    assert m["market_closed"] is False and m["max_leverage"] == 3
