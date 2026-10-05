import time
from datetime import timedelta
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from sqlmodel import Session, select

from app import solana_client as sol
from app.config import settings
from app.db import engine
from app.models import Action, ChainTransfer, Strategy, StrategyDeposit, WatcherCursor, now
from app.state import state
from app.strategies import attest as att
from app.strategies import service, watcher
from auth_helpers import Signer

D = Decimal
OWNER = Signer()
SENDER = str(Keypair().pubkey())
OTHER = str(Keypair().pubkey())
M = "XAU-HL"
# Pyth is mocked at 2650: a 0.12 short = 318 notional, /3x = 106, +20% buffer = 127.2
BODY = dict(fund_id="fund-1", market_id=M, target_exposure_units=0.2, hedge_ratio_bps=6000, leverage=3, rebalance_band_bps=500,
            registered_sender_address=SENDER, expected_amount_usd=127.2, owner_pubkey=OWNER.pubkey)


def create(api, **over):
    r = api.post("/strategies", json={**BODY, **over})
    assert r.status_code == 200, r.text
    return r.json()


def get(api, sid):
    r = api.get(f"/strategies/{sid}")
    assert r.status_code == 200, r.text
    return r.json()


def fund(api, fakechain, s, amount=127.2, memo=None, sender=SENDER):
    sig = fakechain.deposit(sender, amount, memo or s["intent_id"])
    return sig, watcher.watch_once()


def rows(model):
    with Session(engine) as s:
        return list(s.exec(select(model)).all())


@pytest.fixture(autouse=True)
def reset_marks():
    service._mark_cache.clear()
    service._liq_cache.clear()


# ---- create / intent --------------------------------------------------------

def test_create_returns_a_pending_strategy_with_its_intent(api, fakechain):
    s = create(api)
    assert s["status"] == "pending_funding" and s["template"] == "delta_neutral_hedge" and s["position"] is None
    assert s["funding_address"] == fakechain.funding and s["intent_id"] and s["id"] != s["intent_id"]
    assert (s["target_hedge_size_units"], s["required_margin_usd"], s["expected_amount_usd"]) == (0.12, 127.2, 127.2)
    assert s["target_hedge_size_usd"] == pytest.approx(318.0)
    assert s["received_amount_usd"] is None and s["shortfall_usd"] is None and s["market_symbol"] == "XAU"
    assert s["return_wallet_address"] == SENDER and s["fund_name"] == "fund-1" and s["expires_at"].endswith("Z")
    assert get(api, s["id"]) == s


def test_intent_window_is_1h_for_a_wallet_and_7d_for_a_multisig_vault(api):
    pda, _ = Pubkey.find_program_address([b"squads-vault"], Pubkey.from_string(SENDER))
    for sender, seconds in ((SENDER, 3600), (str(pda), 7 * 86400)):
        s = create(api, registered_sender_address=sender)
        with Session(engine) as db:
            st = db.get(Strategy, s["id"])
            assert abs((st.expires_at - st.created_at).total_seconds() - seconds) < 5 and st.multisig == (seconds > 3600)


@pytest.mark.parametrize("over,code,fragment", [
    (dict(market_id="NOPE"), "UNKNOWN_MARKET", "unknown market"),
    (dict(leverage=4), "BAD_REQUEST", "leverage"),
    (dict(leverage=0), "BAD_REQUEST", "leverage"),
    (dict(hedge_ratio_bps=10001), "BAD_REQUEST", "hedge_ratio_bps"),
    (dict(rebalance_band_bps=-1), "BAD_REQUEST", "rebalance_band_bps"),
    (dict(target_exposure_units=0), "BAD_REQUEST", "target_exposure_units"),
    (dict(registered_sender_address="nope"), "BAD_REQUEST", "registered_sender_address"),
    (dict(return_wallet_address="nope"), "BAD_REQUEST", "return_wallet_address"),
    (dict(expected_amount_usd=100), "BAD_REQUEST", "below the required margin 127.2"),
    (dict(expected_amount_usd=0), "BAD_REQUEST", "expected_amount_usd"),
])
def test_create_validation(api, over, code, fragment):
    r = api.post("/strategies", json={**BODY, **over})
    assert r.status_code == 400 and r.json()["code"] == code and fragment in r.json()["error"]
    assert rows(Strategy) == []


def test_create_tolerates_a_small_price_move_in_the_clients_figure(api):
    assert create(api, expected_amount_usd=125.0)["status"] == "pending_funding"  # within 2% of 127.2
    assert api.post("/strategies", json={**BODY, "expected_amount_usd": 120}).status_code == 400


def test_funding_address_endpoint_and_auth(api, fakechain):
    assert api.get("/strategies/funding-address").json() == {"address": fakechain.funding, "stablecoin_mint": fakechain.mint,
                                                              "network": "devnet"}
    for method, path in [("get", "/strategies"), ("post", "/strategies"), ("get", "/strategies/x"), ("post", "/strategies/x/cancel"),
                         ("post", "/strategies/x/deposits"), ("get", "/strategies/funding-address")]:
        assert getattr(api, method)(path, headers={"X-Sereel-Key": "wrong"}).status_code == 401, path


def test_org_scoping_returns_404_for_another_orgs_strategy(api):
    s = api.post("/strategies", json=BODY, headers={"X-Sereel-Org": "org-a", "X-Sereel-User": "u1"}).json()
    assert s["owner_user_id"] == "u1"
    assert api.get(f"/strategies/{s['id']}", headers={"X-Sereel-Org": "org-a"}).status_code == 200
    r = api.get(f"/strategies/{s['id']}", headers={"X-Sereel-Org": "org-b"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert api.post(f"/strategies/{s['id']}/cancel", headers={"X-Sereel-Org": "org-b"}).status_code == 404
    assert [x["id"] for x in api.get("/strategies", headers={"X-Sereel-Org": "org-a"}).json()] == [s["id"]]
    assert api.get("/strategies", headers={"X-Sereel-Org": "org-b"}).json() == []


def test_list_is_a_bare_array_filtered_by_owner_and_fund(api):
    a = api.post("/strategies", json=BODY, headers={"X-Sereel-User": "u1"}).json()
    b = api.post("/strategies", json={**BODY, "fund_id": "fund-2"}, headers={"X-Sereel-User": "u2"}).json()
    assert {x["id"] for x in api.get("/strategies").json()} == {a["id"], b["id"]}
    assert [x["id"] for x in api.get("/strategies", params={"owner": "u1"}).json()] == [a["id"]]
    assert [x["id"] for x in api.get("/strategies", params={"fund_id": "fund-2"}).json()] == [b["id"]]


def test_unknown_strategy_is_404(api):
    assert api.get("/strategies/nope").status_code == 404 and api.post("/strategies/nope/cancel").status_code == 404


# ---- funding rules ----------------------------------------------------------

def test_exact_funding_activates_and_opens_the_short(api, fakechain):
    s = create(api)
    sig, out = fund(api, fakechain, s)
    assert out["transfers"] == 1
    a = get(api, s["id"])
    assert a["status"] == "active" and a["received_amount_usd"] == 127.2 and a["shortfall_usd"] is None
    p = a["position"]
    assert (p["side"], p["size_units"], p["entry_price_usd"], p["margin_usd"]) == ("short", 0.12, 2650.0, 127.2)
    assert p["hl_order_ids"] and p["fees_usd"] > 0 and a["deployed_at"] and 0 < p["margin_health_bps"] <= 10_000
    assert state.venue.position(None, M).size == D("-0.12")  # the real (simulated) account holds the hedge
    assert [o["reduce_only"] for o in state.venue.orders] == [False]  # opening is not reduce-only
    assert state.venue.leverage_status(M) == {"leverage": 3, "mode": "isolated"}  # set before the first order
    with Session(engine) as db:
        assert db.get(Strategy, s["id"]).deploy_signature == sig
        assert rows(ChainTransfer)[0].disposition == "credited"


def test_activation_is_attested_with_a_hash_of_the_full_record(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    import json

    (memo,) = fakechain.memos
    m = json.loads(memo)
    assert set(m) == {"v", "id", "fund", "a", "net", "h"} and (m["id"], m["fund"], m["a"]) == (s["id"], "fund-1", "deploy")
    assert m["net"] == "hyperliquid-testnet" and len(memo.encode()) < 300  # compact
    (act,) = [a for a in rows(Action) if a.action == "deploy"]
    assert att.record_hash(act.record) == m["h"] and act.attestation_sig == "att1"  # the DB record is what the memo commits to
    assert act.record["filled"] == "-0.12" and act.hl_order_ids
    assert get(api, s["id"])["position"] and rows(Strategy)[0].last_attestation_sig == "att1"


def test_overfunding_activates_and_the_excess_becomes_margin(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=200)
    a = get(api, s["id"])
    assert a["status"] == "active" and a["received_amount_usd"] == 200 and a["position"]["margin_usd"] == 200
    assert a["position"]["size_units"] == 0.12  # the hedge size follows exposure x ratio, not the money


def test_underfunding_stays_pending_shows_the_shortfall_and_completes_on_top_up(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=100)
    a = get(api, s["id"])
    assert a["status"] == "pending_funding" and a["received_amount_usd"] == 100 and a["shortfall_usd"] == pytest.approx(27.2)
    assert a["position"] is None and state.venue.position(None, M).size == 0 and fakechain.refunds == []
    fund(api, fakechain, s, amount=27.2)
    a = get(api, s["id"])
    assert a["status"] == "active" and a["received_amount_usd"] == pytest.approx(127.2) and a["shortfall_usd"] is None


@pytest.mark.parametrize("memo,sender,why", [
    (None, SENDER, "no memo"),
    ("not-an-intent", SENDER, "does not match any intent"),
    ("INTENT", OTHER, "registered_sender_address"),  # right memo, wrong sender: must not satisfy someone else's intent
])
def test_unmatched_transfers_are_refunded_to_their_sender_and_attested(api, fakechain, memo, sender, why):
    s = create(api)
    fakechain.deposit(sender, 127.2, s["intent_id"] if memo == "INTENT" else memo)
    watcher.watch_once()
    assert get(api, s["id"])["status"] == "pending_funding" and state.venue.position(None, M).size == 0
    assert fakechain.refunds == [(sender, D("127.2"), fakechain.refunds[0][2])] and "refund" in fakechain.refunds[0][2]
    (t,) = rows(ChainTransfer)
    assert t.disposition == "refunded" and why in t.note and t.refund_signature == "refund1" and t.attestation_sig
    import json

    assert json.loads(fakechain.memos[-1])["a"] == "refund"


def test_a_deposit_after_cancel_is_refunded(api, fakechain):
    s = create(api)
    assert api.post(f"/strategies/{s['id']}/cancel").json()["status"] == "cancelled"
    fund(api, fakechain, s)
    assert get(api, s["id"])["status"] == "cancelled" and len(fakechain.refunds) == 1
    assert "no longer open" in rows(ChainTransfer)[0].note and state.venue.position(None, M).size == 0


def test_a_deposit_after_expiry_is_refunded(api, fakechain):
    s = create(api)
    with Session(engine) as db:
        st = db.get(Strategy, s["id"])
        st.expires_at = now() - timedelta(seconds=1)
        db.add(st)
        db.commit()
    fund(api, fakechain, s)  # the tick handles the transfer first, then expires the intent
    assert len(fakechain.refunds) == 1 and "no longer open" in rows(ChainTransfer)[0].note
    assert get(api, s["id"])["status"] == "expired" and state.venue.position(None, M).size == 0


def test_a_second_deposit_to_an_already_active_intent_is_refunded(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    fund(api, fakechain, s, amount=50)  # same memo again, after activation
    assert fakechain.refunds[0][:2] == (SENDER, D("50")) and get(api, s["id"])["position"]["margin_usd"] == 127.2


def test_transfers_from_the_services_own_addresses_are_ignored_not_refunded(api, fakechain):
    s = create(api)
    for name in ("attest", "payment_source", "mint_authority"):
        fakechain.deposit(str(fakechain.keys[name].pubkey()), 5, s["intent_id"])
    watcher.watch_once()
    assert fakechain.refunds == [] and {t.disposition for t in rows(ChainTransfer)} == {"ignored_own"}
    assert get(api, s["id"])["received_amount_usd"] is None  # not even credited


def test_expiry_refunds_partial_funding(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=60)
    with Session(engine) as db:
        st = db.get(Strategy, s["id"])
        st.expires_at = now() - timedelta(seconds=1)
        db.add(st)
        db.commit()
    assert watcher.watch_once()["expired"] == 1
    assert get(api, s["id"])["status"] == "expired" and fakechain.refunds[0][:2] == (SENDER, D("60"))


def test_cancel_refunds_partial_funding_and_conflicts_once_active(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=60)
    c = api.post(f"/strategies/{s['id']}/cancel").json()
    assert c["status"] == "cancelled" and fakechain.refunds[0][:2] == (SENDER, D("60"))
    again = api.post(f"/strategies/{s['id']}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "CONFLICT"
    s2 = create(api)
    fund(api, fakechain, s2)
    assert api.post(f"/strategies/{s2['id']}/cancel").status_code == 409  # active


# ---- watcher mechanics ------------------------------------------------------

def test_the_same_transfer_is_never_credited_twice(api, fakechain):
    s = create(api, expected_amount_usd=127.2)
    sig, _ = fund(api, fakechain, s, amount=100)
    watcher.process_signature(sig, fakechain.funding)  # replay of the very same signature
    watcher.process_signature(sig, fakechain.funding)
    assert get(api, s["id"])["received_amount_usd"] == 100 and len(rows(ChainTransfer)) == 1


def test_two_workers_racing_on_the_same_transfer_credit_it_once(api, fakechain, monkeypatch):
    """Both pass the early 'seen it?' check (the barrier sits between that check and the claim); only the primary-key
    claim stops the second one from crediting the same money again."""
    import threading

    s = create(api)
    sig = fakechain.deposit(SENDER, 100, s["intent_id"])
    barrier = threading.Barrier(2, timeout=5)
    real = fakechain._get_tx

    def synced(signature, commitment="finalized"):
        tx = real(signature, commitment)
        barrier.wait()
        return tx

    monkeypatch.setattr(sol, "get_parsed_tx", synced)
    errors = []
    threads = [threading.Thread(target=lambda: _try(errors, watcher.process_signature, sig, fakechain.funding)) for _ in range(2)]
    [th.start() for th in threads]
    [th.join() for th in threads]
    assert errors == []
    assert get(api, s["id"])["received_amount_usd"] == 100 and len(rows(ChainTransfer)) == 1  # credited once, not 200


def _try(errors, fn, *a):
    try:
        fn(*a)
    except Exception as e:  # noqa: BLE001
        errors.append(e)


def test_first_start_ignores_history_that_predates_the_service(api, fakechain):
    with Session(engine) as db:  # simulate a first-ever start of a service whose funding wallet already has history
        db.delete(db.get(WatcherCursor, "funding"))
        db.commit()
    old = fakechain.deposit(OTHER, 999)  # an old, unmatched transfer
    s = create(api)
    assert watcher.watch_once()["transfers"] == 0  # baseline: the cursor starts after `old`
    assert fakechain.refunds == [] and rows(ChainTransfer) == []
    with Session(engine) as db:
        assert db.get(WatcherCursor, "funding").last_signature == old
    fund(api, fakechain, s)
    assert get(api, s["id"])["status"] == "active"


def test_the_baseline_is_taken_at_startup_so_an_early_deposit_is_not_lost(api, fakechain):
    with Session(engine) as db:
        assert db.get(WatcherCursor, "funding") is not None  # created by the app's startup, before any request
    s = create(api)
    fakechain.deposit(SENDER, 127.2, s["intent_id"])  # lands before the first watcher tick ever runs
    assert watcher.watch_once()["transfers"] == 1 and get(api, s["id"])["status"] == "active"


def test_the_cursor_survives_a_restart_and_nothing_is_reprocessed(api, fakechain):
    s = create(api)
    fund(api, fakechain, s, amount=100)
    assert watcher.watch_once()["transfers"] == 0  # nothing new
    with Session(engine) as db:
        assert db.get(WatcherCursor, "funding").last_signature == fakechain.order[-1]
    fund(api, fakechain, s, amount=27.2)  # a "restarted" watcher (fresh calls, state only in the DB) picks up the new one
    assert get(api, s["id"])["status"] == "active" and len(rows(ChainTransfer)) == 2


def test_a_failed_fetch_keeps_the_cursor_and_is_retried_next_tick(api, fakechain):
    s = create(api)
    sig = fakechain.deposit(SENDER, 127.2, s["intent_id"])
    fakechain.fetch_errors.add(sig)
    with pytest.raises(sol.SolanaError):
        watcher.watch_once()
    assert get(api, s["id"])["status"] == "pending_funding"
    fakechain.fetch_errors.clear()
    assert watcher.watch_once()["transfers"] == 1 and get(api, s["id"])["status"] == "active"


def test_outgoing_transfers_and_non_stablecoin_activity_are_skipped_silently(api, fakechain):
    s = create(api)
    fakechain.n += 1
    fakechain.txs["sol1"] = {"meta": {"err": None, "innerInstructions": [], "preTokenBalances": [], "postTokenBalances": []},
                             "transaction": {"message": {"instructions": []}}}
    fakechain.order.append("sol1")
    assert watcher.watch_once()["transfers"] == 1 and rows(ChainTransfer) == [] and fakechain.refunds == []


def test_a_failed_refund_is_recorded_and_surfaces_in_health(api, fakechain):
    fakechain.refund_fails = True
    s = create(api)
    fakechain.deposit(OTHER, 10)
    watcher.watch_once()
    (t,) = rows(ChainTransfer)
    assert t.disposition == "refund_failed" and "refund error" in t.note
    assert api.get("/health").json()["unresolved_transfers"] == 1


def test_a_claim_left_processing_by_a_crash_is_flagged_not_retried(api, fakechain):
    with Session(engine) as db:
        db.add(ChainTransfer(signature="crashed", amount_usd=D(5), disposition="processing", created_at=now() - timedelta(minutes=10)))
        db.commit()
    assert watcher.mark_stale_claims() == 1 and rows(ChainTransfer)[0].disposition == "refund_unconfirmed"
    assert fakechain.refunds == []


# ---- activation failure handling -------------------------------------------

def test_a_price_deviation_delays_activation_then_it_succeeds_without_losing_the_funds(api, fakechain):
    s = create(api)
    state.venue.price_override[M] = D("3000")  # far from Pyth's 2650
    fund(api, fakechain, s)
    a = get(api, s["id"])
    assert a["status"] == "pending_funding" and "PRICE_DEVIATION" in a["failure_reason"] and fakechain.refunds == []
    assert a["received_amount_usd"] == 127.2 and a["shortfall_usd"] == 0  # funded, just not open yet
    del state.venue.price_override[M]
    assert watcher.watch_once()["activations"] == 1
    assert get(api, s["id"])["status"] == "active"


def test_repeated_activation_failure_ends_failed_with_a_refund_and_an_attestation(api, fakechain, monkeypatch):
    monkeypatch.setattr(settings, "max_activation_attempts", 2)
    s = create(api)
    state.venue.price_override[M] = D("3000")
    fund(api, fakechain, s)  # attempt 1
    watcher.watch_once()  # attempt 2 -> fail
    a = get(api, s["id"])
    assert a["status"] == "failed" and "PRICE_DEVIATION" in a["failure_reason"]
    assert fakechain.refunds[0][:2] == (SENDER, D("127.2")) and state.venue.position(None, M).size == 0
    import json

    assert [json.loads(m)["a"] for m in fakechain.memos] == ["deploy_failed", "refund"]
    assert watcher.watch_once()["activations"] == 0  # a failed strategy is not retried again


def test_insufficient_margin_fails_the_strategy_with_a_refund(api, fakechain, monkeypatch):
    monkeypatch.setattr(settings, "max_activation_attempts", 1)
    state.venue.funds = D(10)  # the venue account cannot supply the margin
    s = create(api)
    fund(api, fakechain, s)
    a = get(api, s["id"])
    assert a["status"] == "failed" and "INSUFFICIENT_MARGIN" in a["failure_reason"] and len(fakechain.refunds) == 1


def test_no_fill_at_all_retries_and_never_opens_a_phantom_position(api, fakechain, monkeypatch):
    monkeypatch.setattr(settings, "max_activation_attempts", 2)
    state.venue.fill_fraction = D(0)
    s = create(api)
    fund(api, fakechain, s)
    watcher.watch_once()
    a = get(api, s["id"])
    assert a["status"] == "failed" and "ORDER_NOT_FILLED" in a["failure_reason"] and a["position"] is None


def test_a_partial_fill_activates_with_the_actual_size_not_the_target(api, fakechain):
    state.venue.fill_fraction = D("0.5")
    s = create(api)
    fund(api, fakechain, s)
    a = get(api, s["id"])
    assert a["status"] == "active" and 0 < a["position"]["size_units"] < 0.12  # the gap is left for a rebalance
    assert a["position"]["size_units"] == float(-state.venue.position(None, M).size)  # ledger == venue


def test_activation_is_held_when_the_venue_holds_a_position_the_ledger_does_not_explain(api, fakechain):
    state.venue.set_position("stray", M, D("-0.5"))  # e.g. a crash between a fill and its commit
    s = create(api)
    fund(api, fakechain, s)
    a = get(api, s["id"])
    assert a["status"] == "pending_funding" and fakechain.refunds == []  # neither traded on top of it nor refunded
    with Session(engine) as db:
        assert db.get(Strategy, s["id"]).activation_attempts == 0
    assert state.venue.position(None, M).size == D("-0.5")


def test_a_zero_hedge_ratio_activates_without_an_order(api, fakechain):
    s = create(api, hedge_ratio_bps=0, expected_amount_usd=1)
    fund(api, fakechain, s, amount=1)
    a = get(api, s["id"])
    assert a["status"] == "active" and a["position"]["side"] == "flat" and state.venue.orders == []


def test_a_second_strategy_shares_the_account_and_the_ledger_stays_consistent(api, fakechain):
    other = str(Keypair().pubkey())
    s1, s2 = create(api), create(api, registered_sender_address=other)
    fund(api, fakechain, s1)
    fund(api, fakechain, s2, sender=other)
    assert state.venue.position(None, M).size == D("-0.24")
    assert [get(api, x["id"])["position"]["size_units"] for x in (s1, s2)] == [0.12, 0.12]
    assert api.get("/health").json()["reconciliation"]["sum_strategy_margin_usd"] == pytest.approx(254.4)


# ---- top-ups ----------------------------------------------------------------

def active(api, fakechain):
    s = create(api)
    fund(api, fakechain, s)
    return s


def test_top_up_intent_then_transfer_credits_margin_and_attests(api, fakechain):
    s = active(api, fakechain)
    r = api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 50, "registered_sender_address": SENDER})
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "pending_funding" and d["strategy_id"] == s["id"] and d["amount_usd"] == d["expected_amount_usd"] == 50
    assert d["received_amount_usd"] is None and d["solana_signature"] is None and d["intent_id"] and d["expires_at"].endswith("Z")
    sig, _ = fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=50)
    d2 = api.get(f"/strategies/{s['id']}/deposits/{d['id']}").json()
    assert d2["status"] == "confirmed" and d2["received_amount_usd"] == 50 and d2["solana_signature"] == sig
    assert d2["source_wallet_address"] == SENDER and d2["shortfall_usd"] is None
    a = get(api, s["id"])
    assert a["position"]["margin_usd"] == pytest.approx(177.2) and a["position"]["size_units"] == 0.12  # margin only, no trade
    import json

    assert json.loads(fakechain.memos[-1])["a"] == "deposit" and len(state.venue.orders) == 1
    assert [x["id"] for x in api.get(f"/strategies/{s['id']}/deposits").json()] == [d["id"]]


def test_top_up_underfunded_shows_shortfall_and_overfunded_credits_everything(api, fakechain):
    s = active(api, fakechain)
    d = api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 50, "registered_sender_address": SENDER}).json()
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=20)
    d2 = api.get(f"/strategies/{s['id']}/deposits/{d['id']}").json()
    assert d2["status"] == "pending_funding" and d2["received_amount_usd"] == 20 and d2["shortfall_usd"] == 30
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=40)  # 60 total against 50 wanted
    assert api.get(f"/strategies/{s['id']}/deposits/{d['id']}").json()["status"] == "confirmed"
    assert get(api, s["id"])["position"]["margin_usd"] == pytest.approx(127.2 + 60)


def test_top_up_from_the_wrong_sender_is_refunded(api, fakechain):
    s = active(api, fakechain)
    d = api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 50, "registered_sender_address": SENDER}).json()
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=50, sender=OTHER)
    assert fakechain.refunds[0][:2] == (OTHER, D("50")) and api.get(f"/strategies/{s['id']}/deposits/{d['id']}").json()["status"] == "pending_funding"


def test_top_up_expiry_marks_it_expired_and_refunds_a_partial(api, fakechain):
    s = active(api, fakechain)
    d = api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 50, "registered_sender_address": SENDER}).json()
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=20)
    with Session(engine) as db:
        dep = db.get(StrategyDeposit, d["id"])
        dep.expires_at = now() - timedelta(seconds=1)
        db.add(dep)
        db.commit()
    watcher.watch_once()
    assert api.get(f"/strategies/{s['id']}/deposits/{d['id']}").json()["status"] == "expired"
    assert fakechain.refunds[-1][:2] == (SENDER, D("20")) and get(api, s["id"])["position"]["margin_usd"] == 127.2


def test_top_up_needs_an_active_strategy_and_valid_input(api, fakechain):
    pending = create(api)
    r = api.post(f"/strategies/{pending['id']}/deposits", json={"amount_usd": 5, "registered_sender_address": SENDER})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    s = active(api, fakechain)
    assert api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 0, "registered_sender_address": SENDER}).status_code == 400
    assert api.post(f"/strategies/{s['id']}/deposits", json={"amount_usd": 5, "registered_sender_address": "bad"}).status_code == 400
    assert api.get(f"/strategies/{s['id']}/deposits/nope").status_code == 404


# ---- position maths, health, markets ---------------------------------------

def test_position_maths_for_a_short(api, fakechain):
    s = active(api, fakechain)
    state.venue.price_override[M] = D("2700")
    service._mark_cache.clear()
    p = get(api, s["id"])["position"]
    # short 0.12 @ 2650, mark 2700: unrealized = (2700-2650) * -0.12 = -6
    assert p["unrealized_pnl_usd"] == pytest.approx(-6.0) and p["mark_price_usd"] == 2700.0
    cash = 127.2 - p["fees_usd"]
    assert p["maintenance_margin_usd"] == pytest.approx(0.12 * 2700 * 0.02)
    equity = cash - 6.0
    assert p["margin_health_bps"] == int(10_000 * (equity - p["maintenance_margin_usd"]) / equity)
    assert p["liquidation_price_usd"] == pytest.approx((cash + 2650 * 0.12) / (0.12 * 1.02))  # equity == maintenance there


def test_liquidation_price_is_never_rosier_than_the_venues_own(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    computed = get(api, s["id"])["position"]["liquidation_price_usd"]
    real = state.venue.position
    nearer, further = D(str(computed - 500)), D(str(computed + 500))  # for a short, liquidation is ABOVE entry: lower = nearer

    monkeypatch.setattr(state.venue, "position", lambda *a: _with(real(*a), nearer))  # venue's isolated margin is smaller
    service._liq_cache.clear()
    assert get(api, s["id"])["position"]["liquidation_price_usd"] == pytest.approx(float(nearer))  # the venue's nearer figure wins
    service._liq_cache.clear()
    monkeypatch.setattr(state.venue, "position", lambda *a: _with(real(*a), further))
    assert get(api, s["id"])["position"]["liquidation_price_usd"] == pytest.approx(computed)  # ours is nearer: keep ours
    service._liq_cache.clear()
    monkeypatch.setattr(state.venue, "position", lambda *a: _with(real(*a), None))
    assert get(api, s["id"])["position"]["liquidation_price_usd"] == pytest.approx(computed)  # venue gives none: keep ours


def _with(p, liq):
    p.liquidation_px = liq
    return p


def test_reconciliation_accounts_for_fees_instead_of_hiding_them_in_a_tolerance(api, fakechain):
    s = active(api, fakechain)
    r = api.get("/health").json()["reconciliation"]
    fee = get(api, s["id"])["position"]["fees_usd"]
    assert fee > 0.05  # bigger than the tolerance, so the old "margins <= cash + $1" rule was doing the hiding
    assert r["sum_strategy_margin_usd"] == 127.2 and r["ledger_cash_usd"] == pytest.approx(127.2 - fee)
    assert r["dex_cash_usd"] == pytest.approx(r["ledger_cash_usd"], abs=1e-6) and r["difference_usd"] == pytest.approx(0, abs=1e-6)
    assert r["ok"] is True


def test_reconciliation_flags_a_real_shortfall(api, fakechain):
    active(api, fakechain)
    state.venue._cash[M] -= D(5)  # money that the ledger believes is there is not
    r = api.get("/health").json()["reconciliation"]
    assert r["ok"] is False and r["difference_usd"] == pytest.approx(-5, abs=0.01)


def test_health_asserts_leverage_and_reconciles_margin(api, fakechain):
    before = api.get("/health").json()
    assert before["hyperliquid"]["leverage"][M] == {"configured": 3, "actual": 20, "mode": "cross", "ok": False}  # not set yet
    active(api, fakechain)
    h = api.get("/health").json()
    assert h["hyperliquid"]["leverage"][M] == {"configured": 3, "actual": 3, "mode": "isolated", "ok": True}
    assert h["reconciliation"]["ok"] is True and h["active_strategies"] == 1 and h["unresolved_transfers"] == 0


def test_markets_endpoint_reports_mark_pyth_and_the_closed_flag(api):
    (m,) = api.get("/markets").json()
    assert m["market_id"] == M and m["mark_price_usd"] == 2650.0 and m["pyth_price_usd"] == 2650.0
    assert m["market_closed"] is False and m["deviation_bps"] == 0 and api.get("/markets", headers={"X-Sereel-Key": "x"}).status_code == 401


def test_a_closed_market_demo_still_works_and_is_flagged(api, fakechain, monkeypatch):
    from app import pyth

    monkeypatch.setattr(pyth, "get_price", lambda *a, **k: pyth.PythPrice(D(2650), 0, "f", market_closed=True))
    s = create(api)
    fund(api, fakechain, s)
    a = get(api, s["id"])
    assert a["status"] == "active" and a["market_closed"] is True  # no STALE_PRICE; the flag tells the UI


# ---- MIGRATE_ON_START -------------------------------------------------------

def test_migrate_on_start_false_refuses_an_unmigrated_database(monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    from app.db import check_db_at_head, init_db

    eng = create_engine(f"sqlite:///{tmp_path}/x.db")
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        check_db_at_head(eng)
    init_db(eng)
    check_db_at_head(eng)  # at head: fine


def test_the_api_honours_migrate_on_start(monkeypatch, fakechain):
    from fastapi.testclient import TestClient

    from app import main
    from app.payments import scheduler
    from app.strategies import watcher as w
    import app.db as dbmod

    class NoSched:
        def shutdown(self, wait=False): pass

    monkeypatch.setattr(scheduler, "start", lambda: NoSched())
    monkeypatch.setattr(w, "register", lambda s: None)
    calls = []
    monkeypatch.setattr(main, "init_db", lambda *a: calls.append("migrated"))
    monkeypatch.setattr(main, "check_db_at_head", lambda *a: calls.append("checked"))
    monkeypatch.setattr(settings, "migrate_on_start", True)
    with TestClient(main.app):
        pass
    monkeypatch.setattr(settings, "migrate_on_start", False)
    with TestClient(main.app):
        pass
    assert calls == ["migrated", "checked"]
    monkeypatch.setattr(main, "check_db_at_head", lambda *a: (_ for _ in ()).throw(RuntimeError("out of date")))
    with pytest.raises(RuntimeError, match="out of date"):
        with TestClient(main.app):
            pass
