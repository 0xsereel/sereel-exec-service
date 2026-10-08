import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select

from app.config import settings
from app.db import engine
from app.models import Action, PnlSnapshot, Strategy, UsedNonce, now
from app.state import state
from app.strategies import service, watcher
from auth_helpers import Signer
from test_strategies import BODY, M, OWNER, SENDER, create, fund, get, rows

D = Decimal
# Pyth is mocked at 2650 (a 0.12 oz short opened at 2650)


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


def patch(api, sid, params, signer=OWNER, **kw):
    body = {**params, "authorization": signer.authorization("edit_hedge_settings", sid, params, **kw)}
    return api.patch(f"/strategies/{sid}", json=body), body


def rebalance(api, sid, signer=OWNER, force=False, **kw):
    body = {"authorization": signer.authorization("rebalance", sid, {}, **kw)}
    return api.post(f"/strategies/{sid}/rebalance", json=body, params={"force": "true"} if force else {}), body


def actions(sid, kind=None):
    with Session(engine) as db:
        q = select(Action).where(Action.strategy_id == sid).order_by(Action.created_at, Action.id)
        return [a for a in db.exec(q).all() if kind is None or a.action == kind]


def snapshots(sid):
    with Session(engine) as db:
        return list(db.exec(select(PnlSnapshot).where(PnlSnapshot.strategy_id == sid).order_by(PnlSnapshot.id)).all())


def nonces():
    with Session(engine) as db:
        return len(db.exec(select(UsedNonce)).all())


# ---- the fill ledger ---------------------------------------------------------

def st(size="0", entry="0", realized="0", fees="0"):
    return Strategy(fund_id="f", market_id=M, hedge_ratio_bps=1, leverage=1, rebalance_band_bps=1, return_wallet_address="w",
                    registered_sender_address="s", expires_at=now(), size=D(size), entry_px=D(entry),
                    realized_pnl_usd=D(realized), fees_usd=D(fees))


def test_opening_a_position_sets_the_entry():
    s = st()
    assert service.apply_fill(s, D("-0.12"), D(2650), D("0.1")) == 0
    assert (s.size, s.entry_px, s.fees_usd) == (D("-0.12"), D(2650), D("0.1"))


def test_growing_blends_the_entry_price():
    s = st("-0.12", "2650")
    service.apply_fill(s, D("-0.06"), D(2660), D(0))
    assert s.size == D("-0.18") and s.entry_px == (D("0.12") * 2650 + D("0.06") * 2660) / D("0.18")


def test_shrinking_a_short_realizes_a_gain_when_the_price_fell():
    s = st("-0.12", "2650")
    r = service.apply_fill(s, D("0.06"), D(2640), D("0.05"))
    assert r == D("0.6") and s.realized_pnl_usd == D("0.6") and s.size == D("-0.06") and s.entry_px == D(2650)  # entry unchanged
    assert service.apply_fill(s, D("0.06"), D(2700), D(0)) == D("-3.0")  # closing the rest at a loss
    assert s.size == 0 and s.entry_px == 0 and s.realized_pnl_usd == D("-2.4")


def test_shrinking_a_long_realizes_the_opposite_sign():
    s = st("0.1", "100")
    assert service.apply_fill(s, D("-0.04"), D(110), D(0)) == D("0.4")  # a long gains when the price rises
    assert service.apply_fill(s, D("-0.06"), D(90), D(0)) == D("-0.6")


def test_flipping_through_zero_realizes_the_closed_part_and_reopens_at_the_fill_price():
    s = st("-0.1", "100")
    r = service.apply_fill(s, D("0.15"), D(90), D(0))  # closes 0.1 short (+1.0), opens 0.05 long at 90
    assert r == D("1.0") and s.size == D("0.05") and s.entry_px == D(90)


def test_a_zero_fill_changes_nothing():
    s = st("-0.12", "2650", "1", "2")
    assert service.apply_fill(s, D(0), D(1), D(0)) == 0 and (s.size, s.entry_px, s.realized_pnl_usd, s.fees_usd) == (D("-0.12"), D(2650), 1, 2)


# ---- PATCH: move the target, trade nothing -------------------------------------

def test_patch_changes_the_target_only_and_reports_the_gap(api, fakechain):
    s = active(api, fakechain)
    orders, memos = len(state.venue.orders), len(fakechain.memos)
    r, _ = patch(api, s["id"], {"target_exposure_units": "0.3"})
    assert r.status_code == 200
    a = r.json()
    assert a["target_hedge_size_units"] == 0.18 and a["hedge_ratio_bps"] == 6000 and a["target_exposure_units"] == 0.3
    assert a["position"]["size_units"] == 0.12  # nothing was traded
    assert a["hedge_gap_units"] == pytest.approx(0.06) and a["hedge_gap_bps"] == 3333  # under-hedged by a third
    assert a["required_margin_usd"] == pytest.approx(0.18 * 2650 / 3 * 1.2)  # recomputed for the new size
    assert len(state.venue.orders) == orders
    (act,) = actions(s["id"], "edit_hedge_settings")
    assert act.signer_public_key == OWNER.pubkey and act.authorization["nonce"] and act.record["from"]["target_exposure_units"] == "0.2"
    assert json.loads(fakechain.memos[-1])["a"] == "edit_hedge_settings" and len(fakechain.memos) == memos + 1
    assert snapshots(s["id"])[-1].cause == "edit_hedge_settings"


def test_patch_accepts_either_field_alone(api, fakechain):
    s = active(api, fakechain)
    a = patch(api, s["id"], {"hedge_ratio_bps": "3000"})[0].json()
    assert a["target_hedge_size_units"] == 0.06 and a["hedge_ratio_bps"] == 3000
    assert a["hedge_gap_units"] == pytest.approx(-0.06) and a["hedge_gap_bps"] == 10_000  # over-hedged: current 0.12 vs target 0.06
    a = patch(api, s["id"], {"target_exposure_units": "1"})[0].json()
    assert a["target_hedge_size_units"] == 0.3 and a["hedge_ratio_bps"] == 3000


def test_patch_validation_happens_before_the_signature_is_consumed(api, fakechain):
    s = active(api, fakechain)
    for params, fragment in (({}, "send hedge_ratio_bps"), ({"hedge_ratio_bps": "10001"}, "between 0 and 10000"),
                             ({"target_exposure_units": "0"}, "must be positive")):
        r, _ = patch(api, s["id"], params)
        assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and fragment in r.json()["error"], params
    assert nonces() == 0 and actions(s["id"], "edit_hedge_settings") == []


def test_numeric_body_fields_are_rejected_before_anything_is_stored(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    before = (get(api, sid)["target_exposure_units"], len(actions(sid)), len(fakechain.memos))
    for body_fields, signed_params in (({"hedge_ratio_bps": 6000}, {"hedge_ratio_bps": "6000"}),  # the body disagrees with what was signed
                                       ({"target_exposure_units": 0.3}, {"target_exposure_units": "0.3"}),
                                       ({"target_exposure_units": 1}, {"target_exposure_units": "1"}),
                                       ({"hedge_ratio_bps": 6000.0}, {"hedge_ratio_bps": "6000"})):
        body = {**body_fields, "authorization": OWNER.authorization("edit_hedge_settings", sid, signed_params)}
        r = api.patch(f"/strategies/{sid}", json=body)
        assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID" and "not a JSON number" in r.json()["error"], body_fields
    assert nonces() == 0  # nothing was consumed
    assert (get(api, sid)["target_exposure_units"], len(actions(sid)), len(fakechain.memos)) == before  # nothing stored or attested


def test_numeric_body_fields_are_rejected_even_under_the_dev_bypass(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    assert api.patch(f"/strategies/{s['id']}", json={"hedge_ratio_bps": 3000}).status_code == 403
    r = api.patch(f"/strategies/{s['id']}", json={"hedge_ratio_bps": "3000"})  # a string passes; no signature needed under bypass
    assert r.status_code == 200 and r.json()["hedge_ratio_bps"] == 3000


def test_the_body_must_match_exactly_what_was_signed(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    body = {"hedge_ratio_bps": "7000", "authorization": OWNER.authorization("edit_hedge_settings", sid, {"hedge_ratio_bps": "6000"})}
    r = api.patch(f"/strategies/{sid}", json=body)
    assert r.status_code == 403 and "does not match this request" in r.json()["error"]
    body = {"hedge_ratio_bps": "6000", "target_exposure_units": "9",
            "authorization": OWNER.authorization("edit_hedge_settings", sid, {"hedge_ratio_bps": "6000"})}  # an extra, unsigned field
    assert api.patch(f"/strategies/{sid}", json=body).status_code == 403
    assert get(api, sid)["target_exposure_units"] == 0.2


def test_patch_needs_the_owners_signature_and_an_active_strategy(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    assert api.patch(f"/strategies/{sid}", json={"hedge_ratio_bps": "3000"}).status_code == 401
    r, _ = patch(api, sid, {"hedge_ratio_bps": "3000"}, signer=Signer())
    assert r.status_code == 403 and "not the strategy's owner" in r.json()["error"]
    r, body = patch(api, sid, {"hedge_ratio_bps": "3000"})
    assert r.status_code == 200
    assert api.patch(f"/strategies/{sid}", json=body).status_code == 403  # replay
    pending = create(api)
    r, _ = patch(api, pending["id"], {"hedge_ratio_bps": "3000"})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert api.patch("/strategies/nope", json={"hedge_ratio_bps": "3000"}).status_code == 404
    assert get(api, sid)["hedge_ratio_bps"] == 3000


# ---- rebalance ------------------------------------------------------------------

def test_rebalance_inside_the_band_does_nothing_and_attests_nothing(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.205"})  # gap = 0.003 / 0.123 = 2.4% < the 5% band
    orders, memos = len(state.venue.orders), len(fakechain.memos)
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 200 and r.json()["position"]["size_units"] == 0.12 and r.json()["status"] == "active"
    assert len(state.venue.orders) == orders and len(fakechain.memos) == memos  # no trade, no attestation
    (noop,) = actions(s["id"], "rebalance")
    assert noop.record["traded"] is False and "within the 500 bps band" in noop.record["reason"] and noop.attestation_sig is None
    assert noop.signer_public_key == OWNER.pubkey  # but the signed request is on record


def test_a_gap_exactly_equal_to_the_band_does_not_trade_but_one_bp_more_does(api, fakechain):
    # exposure 0.2 -> 0.3 makes the target 0.18 against 0.12: a gap of 3333 bps
    s = create(api, rebalance_band_bps=3333)
    fund(api, fakechain, s, amount=300)
    patch(api, s["id"], {"target_exposure_units": "0.3"})
    orders = len(state.venue.orders)
    assert rebalance(api, s["id"])[0].json()["hedge_gap_bps"] == 3333 and len(state.venue.orders) == orders  # equal: inside the band
    s2 = create(api, rebalance_band_bps=3332, registered_sender_address=str(Keypair().pubkey()))
    fund(api, fakechain, s2, amount=300, sender=s2["registered_sender_address"])
    patch(api, s2["id"], {"target_exposure_units": "0.3"})
    assert rebalance(api, s2["id"])[0].json()["position"]["size_units"] == pytest.approx(0.18)  # one bp beyond: trades


def test_force_trades_even_inside_the_band(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.21"})  # target 0.126: a 4.8% gap, inside the band, and a $15 trade (viable)
    r, _ = rebalance(api, s["id"], force=True)
    assert r.json()["position"]["size_units"] == 0.126 and r.json()["hedge_gap_units"] == pytest.approx(0)
    assert actions(s["id"], "rebalance")[0].record["forced"] is True


def test_rebalance_down_is_reduce_only_and_books_realized_pnl(api, fakechain):
    s = active(api, fakechain)
    fee_open = get(api, s["id"])["position"]["fees_usd"]
    set_price(2640)
    patch(api, s["id"], {"target_exposure_units": "0.1"})  # target 0.06
    r, _ = rebalance(api, s["id"])
    p = r.json()["position"]
    assert r.status_code == 200 and p["size_units"] == pytest.approx(0.06) and p["entry_price_usd"] == 2650.0  # entry unchanged
    assert p["realized_pnl_usd"] == pytest.approx(0.6)  # (2650 - 2640) * 0.06 on the part closed
    assert p["fees_usd"] > fee_open and r.json()["hedge_gap_units"] == pytest.approx(0) and r.json()["status"] == "active"
    last = state.venue.orders[-1]
    assert last["is_buy"] is True and last["reduce_only"] is True  # shrinking never opens or grows anything
    (act,) = actions(s["id"], "rebalance")
    assert act.record["traded"] is True and act.record["reduce_only"] == [True] and act.hl_order_ids
    m = json.loads(fakechain.memos[-1])
    assert m["a"] == "rebalance" and act.attestation_sig and act.signer_public_key == OWNER.pubkey
    assert snapshots(s["id"])[-1].cause == "rebalance"


def test_rebalance_up_blends_the_entry_and_is_not_reduce_only(api, fakechain):
    s = active(api, fakechain, amount=300)  # plenty of capital behind the strategy
    set_price(2660)
    patch(api, s["id"], {"target_exposure_units": "0.3"})  # target 0.18
    p = rebalance(api, s["id"])[0].json()["position"]
    assert p["size_units"] == pytest.approx(0.18)
    assert p["entry_price_usd"] == pytest.approx(float((D("0.12") * 2650 + D("0.06") * 2660) / D("0.18")))
    assert state.venue.orders[-1]["reduce_only"] is False and state.venue.orders[-1]["is_buy"] is False


def test_growing_without_enough_capital_is_insufficient_margin_and_changes_nothing(api, fakechain):
    s = active(api, fakechain)  # 127.2 of margin backs a 0.12 short; 0.18 at 3x needs ~159
    patch(api, s["id"], {"target_exposure_units": "0.3"})
    orders = len(state.venue.orders)
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 400 and r.json()["code"] == "INSUFFICIENT_MARGIN" and "top-up" in r.json()["error"]
    a = get(api, s["id"])
    assert a["status"] == "active" and a["position"]["size_units"] == 0.12 and len(state.venue.orders) == orders


def test_a_partial_fill_leaves_a_visible_gap(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    state.venue.fill_fraction = D("0.5")
    a = rebalance(api, s["id"])[0].json()
    assert a["status"] == "active" and 0.06 < a["position"]["size_units"] < 0.12  # closed some, not all
    assert a["hedge_gap_units"] < 0 and abs(a["hedge_gap_units"]) > 0
    assert actions(s["id"], "rebalance")[0].record["remaining"] != "0"


def test_no_fill_leaves_everything_as_it_was(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    before = get(api, s["id"])["position"]
    state.venue.fill_fraction = D(0)
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 400 and r.json()["code"] == "ORDER_NOT_FILLED"
    a = get(api, s["id"])
    assert a["status"] == "active" and a["position"]["size_units"] == before["size_units"] and a["position"]["realized_pnl_usd"] == 0
    assert actions(s["id"], "rebalance") == []


def test_a_price_deviation_blocks_the_rebalance(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    set_price(3000)
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 400 and r.json()["code"] == "PRICE_DEVIATION" and get(api, s["id"])["status"] == "active"


def test_rebalance_is_held_while_the_ledger_and_the_venue_disagree(api, fakechain):
    s = active(api, fakechain)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    state.venue.set_position("stray", M, D("-0.9"))  # a position the ledger cannot explain
    orders = len(state.venue.orders)
    r, _ = rebalance(api, s["id"])
    assert r.status_code == 409 and "reconciled" in r.json()["error"] and len(state.venue.orders) == orders
    assert get(api, s["id"])["status"] == "active"


def test_rebalance_needs_a_signature_the_owner_and_an_active_strategy(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    assert api.post(f"/strategies/{sid}/rebalance", json={}).status_code == 401
    r, _ = rebalance(api, sid, signer=Signer())
    assert r.status_code == 403
    r, body = rebalance(api, sid)
    assert r.status_code == 200
    assert api.post(f"/strategies/{sid}/rebalance", json=body).status_code == 403  # replay
    used = nonces()
    assert rebalance(api, create(api)["id"])[0].status_code == 409  # a pending strategy
    assert nonces() == used  # refused on state before the signature was spent
    assert api.post("/strategies/nope/rebalance", json={}).status_code == 404


def test_an_interrupted_rebalance_is_recovered_at_startup(api, fakechain):
    s = active(api, fakechain)
    with Session(engine) as db:
        x = db.get(Strategy, s["id"])
        x.status = "rebalancing"
        db.add(x)
        db.commit()
    assert service.recover_interrupted() == 1 and get(api, s["id"])["status"] == "active"


# ---- funding ----------------------------------------------------------------------

def test_funding_is_booked_by_size_share_and_never_counted_twice(api, fakechain):
    other = str(Keypair().pubkey())
    s1 = active(api, fakechain)
    s2 = create(api, registered_sender_address=other)
    fund(api, fakechain, s2, sender=other)
    t = int(time.time() * 1000)
    state.venue.funding_log += [(1, D("-5")), (t + 10_000, D("-0.24")), (t + 20_000, D("0.04"))]  # the first predates the position
    for sid in (s1["id"], s2["id"]):
        service.accrue_funding(sid)
    for sid in (s1["id"], s2["id"]):
        assert get(api, sid)["position"]["funding_paid_usd"] == pytest.approx(0.10)  # each paid half of the net 0.20
        service.accrue_funding(sid)  # again: the cursor moved, so nothing new
        assert get(api, sid)["position"]["funding_paid_usd"] == pytest.approx(0.10)
    v = api.get(f"/strategies/{s1['id']}/value").json()
    assert v["funding_usd"] == pytest.approx(-0.10)  # net received is negative when paid
    assert v["hedge_pnl_usd"] == pytest.approx(v["unrealized_pnl_usd"] + v["realized_pnl_usd"] + v["funding_usd"] - v["fees_usd"])


def test_funding_is_only_booked_for_strategies_holding_a_position(api, fakechain):
    pending = create(api)
    state.venue.funding_log.append((int(time.time() * 1000) + 10_000, D("-1")))
    assert service.accrue_funding(pending["id"]) == 0


# ---- snapshots -----------------------------------------------------------------------

def test_snapshots_are_taken_on_every_action(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    assert [x.cause for x in snapshots(sid)] == ["deploy"]
    d = api.post(f"/strategies/{sid}/deposits", json={"amount_usd": 10, "registered_sender_address": SENDER}).json()
    fund(api, fakechain, {"intent_id": d["intent_id"]}, amount=10)
    patch(api, sid, {"target_exposure_units": "0.1"})
    rebalance(api, sid)
    assert [x.cause for x in snapshots(sid)] == ["deploy", "deposit", "edit_hedge_settings", "rebalance"]


def test_the_minute_job_snapshots_only_strategies_that_hold_a_position(api, fakechain):
    live = active(api, fakechain)
    pending = create(api)
    cancelled = create(api)
    api.post(f"/strategies/{cancelled['id']}/cancel")
    state.venue.funding_log.append((int(time.time() * 1000) + 10_000, D("-0.12")))
    assert service.snapshot_all() == 1
    assert [x.cause for x in snapshots(live["id"])][-1] == "tick" and snapshots(pending["id"]) == [] and snapshots(cancelled["id"]) == []
    assert snapshots(live["id"])[-1].funding_usd == D("-0.12")  # funding was accrued first


def test_the_scheduler_registers_the_minute_job(monkeypatch):
    added = []

    class Sched:
        def add_job(self, fn, trigger, **kw):
            added.append((kw["id"], kw.get("seconds")))

    watcher.register(Sched())
    assert ("pnl-snapshots", 60) in added and ("deposit-watcher", 5) in added


def test_a_snapshot_is_skipped_rather_than_faked_when_there_is_no_price(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    n = len(snapshots(s["id"]))
    monkeypatch.setattr(state.venue, "mark_price", lambda m: (_ for _ in ()).throw(RuntimeError("venue down")))
    assert service.take_snapshot(s["id"], "tick") is False and len(snapshots(s["id"])) == n


# ---- /value --------------------------------------------------------------------------

def test_value_has_every_field_independently_populated(api, fakechain):
    s = active(api, fakechain)
    sid = s["id"]
    set_price(2640)
    patch(api, sid, {"target_exposure_units": "0.1"})
    rebalance(api, sid)  # realizes P&L and pays fees
    state.venue.funding_log.append((int(time.time() * 1000) + 10_000, D("-0.06")))
    service.accrue_funding(sid)
    set_price(2630)  # and an unrealized move on what is left
    r = api.get(f"/strategies/{sid}/value")
    assert r.status_code == 200
    v = r.json()
    assert set(v) >= {"margin_usd", "unrealized_pnl_usd", "realized_pnl_usd", "funding_usd", "fees_usd", "value_usd", "as_of"}
    assert all(isinstance(v[k], float) for k in ("margin_usd", "unrealized_pnl_usd", "realized_pnl_usd", "funding_usd", "fees_usd", "value_usd"))
    assert v["margin_usd"] == 127.2
    assert v["unrealized_pnl_usd"] == pytest.approx(1.2)  # (2650 - 2630) * 0.06, short
    assert v["realized_pnl_usd"] == pytest.approx(0.6) and v["funding_usd"] == pytest.approx(-0.06) and v["fees_usd"] > 0
    assert min(abs(v[k]) for k in ("unrealized_pnl_usd", "realized_pnl_usd", "funding_usd", "fees_usd")) > 0  # none is a hardcoded 0
    assert v["value_usd"] == pytest.approx(v["margin_usd"] + v["unrealized_pnl_usd"])  # the literal v4 definition
    assert v["hedge_pnl_usd"] == pytest.approx(v["unrealized_pnl_usd"] + v["realized_pnl_usd"] + v["funding_usd"] - v["fees_usd"])
    assert v["hedge_pnl_usd"] != pytest.approx(v["hedge_pnl_usd"] + v["margin_usd"])  # margin is never inside hedge P&L
    assert v["as_of"].endswith("Z") and v["attestation_sig"] and "explorer.solana.com" in v["attestation_url"]


def test_value_of_a_strategy_without_a_position_is_zero_not_an_error(api, fakechain):
    pending = create(api)
    v = api.get(f"/strategies/{pending['id']}/value").json()
    assert (v["margin_usd"], v["unrealized_pnl_usd"], v["realized_pnl_usd"], v["funding_usd"], v["fees_usd"], v["value_usd"]) == (0,) * 6
    assert api.get("/strategies/nope/value").status_code == 404


def test_value_reports_a_venue_outage_instead_of_a_wrong_number(api, fakechain, monkeypatch):
    s = active(api, fakechain)
    monkeypatch.setattr(service, "mark_for", lambda m: None)
    r = api.get(f"/strategies/{s['id']}/value")
    assert r.status_code == 503 and r.json()["code"] == "VENUE_UNAVAILABLE" and set(r.json()) == {"error", "code"}


def test_value_requires_the_api_key_and_respects_org_scope(api, fakechain):
    s = api.post("/strategies", json=BODY, headers={"X-Sereel-Org": "org-a"}).json()
    assert api.get(f"/strategies/{s['id']}/value", headers={"X-Sereel-Key": "bad"}).status_code == 401
    assert api.get(f"/strategies/{s['id']}/value", headers={"X-Sereel-Org": "org-b"}).status_code == 404
    assert api.get(f"/strategies/{s['id']}/value", headers={"X-Sereel-Org": "org-a"}).status_code == 200


# ---- /value?as_of -------------------------------------------------------------------------

def seeded(api, fakechain):
    """An active strategy whose snapshots are replaced by three known ones, a minute apart."""
    s = active(api, fakechain)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    with Session(engine) as db:
        for snap in db.exec(select(PnlSnapshot).where(PnlSnapshot.strategy_id == s["id"])).all():
            db.delete(snap)
        for i, hedge in enumerate((1, 2, 3)):
            db.add(PnlSnapshot(strategy_id=s["id"], ts=t0 + timedelta(minutes=i), unrealized_pnl_usd=D(hedge), realized_pnl_usd=D(10 * hedge),
                               funding_usd=D(-hedge), fees_usd=D("0.5"), hedge_pnl_usd=D(hedge) + 10 * hedge - hedge - D("0.5"),
                               margin_usd=D(100 + i), cause="tick"))
        for a in db.exec(select(Action).where(Action.strategy_id == s["id"])).all():
            db.delete(a)
        db.add(Action(strategy_id=s["id"], action="deploy", attestation_sig="att-A", created_at=t0 - timedelta(seconds=5)))
        db.add(Action(strategy_id=s["id"], action="rebalance", attestation_sig="att-B", created_at=t0 + timedelta(seconds=90)))
        db.commit()
    return s, t0


def test_as_of_returns_the_snapshot_at_or_just_before_the_time(api, fakechain):
    s, t0 = seeded(api, fakechain)
    sid = s["id"]
    for when, expect_margin, expect_ts in (
            ("2026-01-01T12:00:00Z", 100, "2026-01-01T12:00:00Z"),  # exactly on a snapshot
            ("2026-01-01T12:00:59Z", 100, "2026-01-01T12:00:00Z"),  # just before the next one
            ("2026-01-01T12:01:00Z", 101, "2026-01-01T12:01:00Z"),
            ("2026-01-01T12:01:30Z", 101, "2026-01-01T12:01:00Z"),
            ("2026-01-01T12:02:00Z", 102, "2026-01-01T12:02:00Z"),
            ("2030-01-01T00:00:00Z", 102, "2026-01-01T12:02:00Z")):  # after the last: the latest
        r = api.get(f"/strategies/{sid}/value", params={"as_of": when})
        assert r.status_code == 200, when
        v = r.json()
        assert v["margin_usd"] == expect_margin and v["as_of"] == expect_ts, when
    v = api.get(f"/strategies/{sid}/value", params={"as_of": "2026-01-01T12:01:30Z"}).json()
    assert (v["unrealized_pnl_usd"], v["realized_pnl_usd"], v["funding_usd"], v["fees_usd"]) == (2, 20, -2, 0.5)
    assert v["hedge_pnl_usd"] == pytest.approx(2 + 20 - 2 - 0.5) and v["value_usd"] == 101 + 2


def test_as_of_before_the_first_snapshot_is_a_404(api, fakechain):
    s, _ = seeded(api, fakechain)
    r = api.get(f"/strategies/{s['id']}/value", params={"as_of": "2025-12-31T23:59:59Z"})
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND" and "no snapshot" in r.json()["error"]


def test_as_of_accepts_offsets_naive_times_and_unix_milliseconds(api, fakechain):
    s, t0 = seeded(api, fakechain)
    sid = s["id"]
    ask = lambda x: api.get(f"/strategies/{sid}/value", params={"as_of": x})
    assert ask("2026-01-01T14:01:30+02:00").json()["margin_usd"] == 101  # 12:01:30 UTC
    assert ask("2026-01-01T12:01:30").json()["margin_usd"] == 101  # naive is UTC
    assert ask("2026-01-01T12:01:30+00:00").json()["margin_usd"] == 101
    ms = int((t0 + timedelta(seconds=75)).timestamp() * 1000)
    assert ask(str(ms)).json()["margin_usd"] == 101
    for bad in ("yesterday", "", "2026-13-45", "12:00"):
        r = ask(bad)
        assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST", bad


def test_as_of_is_unaffected_by_what_the_strategy_does_afterwards(api, fakechain):
    s, _ = seeded(api, fakechain)
    sid = s["id"]
    first = api.get(f"/strategies/{sid}/value", params={"as_of": "2026-01-01T12:01:30Z"}).json()
    set_price(2600)
    patch(api, sid, {"target_exposure_units": "0.1"})
    rebalance(api, sid)
    assert api.get(f"/strategies/{sid}/value", params={"as_of": "2026-01-01T12:01:30Z"}).json() == first  # history is history


def test_as_of_links_the_attestation_in_force_at_that_time(api, fakechain):
    s, _ = seeded(api, fakechain)
    sid = s["id"]
    get_ = lambda x: api.get(f"/strategies/{sid}/value", params={"as_of": x}).json()["attestation_sig"]
    assert get_("2026-01-01T12:00:30Z") == "att-A" and get_("2026-01-01T12:01:30Z") == "att-A"  # snapshot at 12:01:00 < att-B at 12:01:30
    assert get_("2026-01-01T12:02:30Z") == "att-B"


# ---- /history ---------------------------------------------------------------------------------

def test_history_lists_every_action_in_order_with_fills_and_signatures(api, fakechain):
    s = create(api)
    sig, _ = fund(api, fakechain, s)
    set_price(2640)
    patch(api, s["id"], {"target_exposure_units": "0.1"})
    rebalance(api, s["id"])
    new = Signer()
    body = {"owner_pubkey": new.pubkey, "authorization": OWNER.authorization("change_owner", s["id"], {"owner_pubkey": new.pubkey})}
    assert api.post(f"/strategies/{s['id']}/owner", json=body).status_code == 200
    h = api.get(f"/strategies/{s['id']}/history").json()
    assert isinstance(h, list) and [x["type"] for x in h] == ["create", "deploy", "edit_hedge_settings", "rebalance", "change_owner"]
    create_, deploy, edit, reb, owner = h
    assert create_["attestation_signature"] is None and create_["signed_by"] is None  # not attested, not signed
    assert deploy["solana_signature"] == sig  # the funding transfer, as v4 expects the deploy to surface
    assert deploy["fill"]["size"] == -0.12 and deploy["fill"]["avg_price_usd"] == 2650.0 and deploy["fee_usd"] > 0
    assert deploy["hl_order_ids"] and deploy["attestation_signature"] and "explorer.solana.com" in deploy["attestation_url"]
    assert edit["signed_by"] == OWNER.pubkey and edit["attestation_signature"]
    assert reb["fill"]["size"] == 0.06 and reb["realized_pnl_usd"] == pytest.approx(0.6) and reb["signed_by"] == OWNER.pubkey
    assert reb["hl_order_ids"] and owner["details"]["to"]["owner_pubkey"] == new.pubkey
    assert all(x["created_at"].endswith("Z") for x in h) and [x["created_at"] for x in h] == sorted(x["created_at"] for x in h)


def test_history_is_org_scoped_and_404_for_unknown(api, fakechain):
    s = api.post("/strategies", json=BODY, headers={"X-Sereel-Org": "org-a"}).json()
    assert api.get(f"/strategies/{s['id']}/history", headers={"X-Sereel-Org": "org-b"}).status_code == 404
    assert api.get("/strategies/nope/history").status_code == 404
    assert api.get(f"/strategies/{s['id']}/history", headers={"X-Sereel-Key": "bad"}).status_code == 401
    assert [x["type"] for x in api.get(f"/strategies/{s['id']}/history", headers={"X-Sereel-Org": "org-a"}).json()] == ["create"]
