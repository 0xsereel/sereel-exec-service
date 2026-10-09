"""Addendum v3: custody_proof. A simulated proof is a labelled demonstration: deterministic, never a balance, never ahead of a published NAV,
never in a Solana memo, never called verified."""
import hashlib
import json
import re
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.config import Settings, settings
from app.db import engine
from app.models import Action, NavCheckpoint, now
from app.x402 import custody, feed
from test_x402 import (CUSTOMER, KEYLESS, active, configure, enable, fac, get, nav_params, nav_req, pay, required_of, walk,  # noqa: F401
                       busy_strategy)

NOT_CONFIGURED = {"status": "unavailable", "reason": "custody proof source not configured"}
NOTE = "Simulated proof for demonstration. Not a real bank attestation."
EXPECTED_KEYS = {"mode", "provider", "custodian", "claim", "claim_holds", "covers_nav_as_of", "proof_id", "proof_hash", "generated_at", "note"}


@pytest.fixture
def simulated(monkeypatch):
    monkeypatch.setattr(settings, "custody_proof_mode", "simulated")


def strategy_with_nav(api, fakechain, *offsets):
    sid = active(api, fakechain)["id"]
    for off in offsets:
        r = nav_req(api, sid, nav_params(off, nav=f"1.{abs(off):04d}", unh="1.0100"))
        assert r.status_code == 200, r.text
    return sid


def checkpoints(sid):
    with Session(engine) as db:
        return list(db.exec(select(NavCheckpoint).where(NavCheckpoint.strategy_id == sid).order_by(NavCheckpoint.as_of)).all())


# ---- off: nothing, with the exact reason --------------------------------------------------------------------------------------------------

def test_the_default_is_off_and_the_field_reads_unavailable_with_the_exact_reason(api, fakechain, fac):
    assert settings.custody_proof_mode == "off"
    sid = strategy_with_nav(api, fakechain, -60)
    assert feed.build(sid, ["custody_proof"])["custody_proof"] == NOT_CONFIGURED
    assert checkpoints(sid)[0].custody_proof is None  # nothing was generated, nothing was invented
    hist = feed.build(sid, ["nav_history", "custody_proof"])["nav_history"]["checkpoints"]
    assert hist[0]["custody_proof"] == NOT_CONFIGURED


def test_off_with_no_checkpoint_is_still_just_not_configured(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    assert feed.build(sid, ["custody_proof"])["custody_proof"] == NOT_CONFIGURED  # no source: that is the only thing to say, checkpoint or not


def test_off_hides_even_a_proof_that_was_stored_earlier(api, fakechain, fac, monkeypatch):
    monkeypatch.setattr(settings, "custody_proof_mode", "simulated")
    sid = strategy_with_nav(api, fakechain, -60)
    assert feed.build(sid, ["custody_proof"])["custody_proof"]["mode"] == "simulated"
    monkeypatch.setattr(settings, "custody_proof_mode", "off")
    assert feed.build(sid, ["custody_proof"])["custody_proof"] == NOT_CONFIGURED


# ---- simulated ---------------------------------------------------------------------------------------------------------------------------------

def test_a_simulated_proof_has_the_agreed_shape_and_always_says_it_is_simulated(api, fakechain, fac, simulated):
    sid = strategy_with_nav(api, fakechain, -60)
    cp = checkpoints(sid)[0]
    proof = feed.build(sid, ["custody_proof"])["custody_proof"]
    assert set(proof) == EXPECTED_KEYS
    assert (proof["mode"], proof["provider"], proof["custodian"], proof["claim"], proof["claim_holds"]) == (
        "simulated", "zkTLS", "Standard Chartered (Straight2Bank)", "custody cash balance >= reported fund NAV", True)
    assert proof["note"] == NOTE and proof["covers_nav_as_of"] == feed.iso(cp.as_of)
    assert proof["proof_id"] == "sim-" + hashlib.sha256((sid + feed.iso(cp.as_of)).encode()).hexdigest()[:12]
    assert re.fullmatch(r"sim-[0-9a-f]{12}", proof["proof_id"]) and re.fullmatch(r"[0-9a-f]{64}", proof["proof_hash"])
    assert proof["generated_at"] == feed.iso(cp.created_at) and proof["generated_at"].endswith("Z")
    body = {k: v for k, v in proof.items() if k != "proof_hash"}
    assert proof["proof_hash"] == hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()  # the hash covers the record


def test_the_proof_is_deterministic(simulated):
    a = custody.generate("strat-1", "2026-10-15T09:00:00Z", "2026-10-15T09:00:05Z")
    b = custody.generate("strat-1", "2026-10-15T09:00:00Z", "2026-10-15T09:00:05Z")
    assert a == b and a["proof_hash"] == b["proof_hash"]
    assert custody.generate("strat-2", "2026-10-15T09:00:00Z", "2026-10-15T09:00:05Z")["proof_id"] != a["proof_id"]
    assert custody.generate("strat-1", "2026-10-15T09:01:00Z", "2026-10-15T09:00:05Z")["proof_id"] != a["proof_id"]
    assert custody.generate("strat-1", "2026-10-15T09:00:00Z", "2026-10-15T09:00:05Z")["claim_holds"] is True


def test_a_proof_never_reveals_a_balance_an_account_or_any_bank_data(api, fakechain, fac, simulated):
    sid = strategy_with_nav(api, fakechain, -60)
    proof = feed.build(sid, ["custody_proof"])["custody_proof"]
    assert set(proof) == EXPECTED_KEYS  # an exact whitelist: there is no field a balance could ride in
    text = json.dumps(proof).lower()
    for word in ("balance_usd", "account_number", "iban", "swift", "routing", "sort_code", "holder"):
        assert word not in text
    assert "1.0" not in re.sub(r'"(proof_id|proof_hash|generated_at|covers_nav_as_of)": "[^"]*"', "", json.dumps(proof))  # nor the NAV itself


def test_no_proof_exists_without_a_published_checkpoint_never_before_never_ahead(api, fakechain, fac, simulated):
    sid = active(api, fakechain)["id"]
    assert feed.build(sid, ["custody_proof"])["custody_proof"] == {"status": "unavailable", "reason": "no NAV has been published for this strategy yet"}
    assert checkpoints(sid) == []
    nav_req(api, sid, nav_params(-60))
    cp = checkpoints(sid)[0]
    proof = feed.build(sid, ["custody_proof"])["custody_proof"]
    assert proof["covers_nav_as_of"] == feed.iso(cp.as_of) and proof["generated_at"] >= feed.iso(cp.created_at)  # made at publication, for that NAV
    assert proof["generated_at"] == feed.iso(cp.created_at)  # not a moment sooner than the checkpoint existed


def test_a_rejected_nav_publication_leaves_no_proof_behind(api, fakechain, fac, simulated):
    sid = active(api, fakechain)["id"]
    assert nav_req(api, sid, nav_params(+31)).status_code == 400  # a NAV from the future is refused...
    assert nav_req(api, sid, nav_params(-60, nav="0")).status_code == 400
    assert checkpoints(sid) == [] and feed.build(sid, ["custody_proof"])["custody_proof"]["status"] == "unavailable"  # ...and nothing is proven


def test_the_latest_checkpoint_supplies_custody_proof_and_every_history_entry_carries_its_own(api, fakechain, fac, simulated):
    sid = strategy_with_nav(api, fakechain, -300, -200, -100)
    cps = checkpoints(sid)
    body = feed.build(sid, ["nav_history", "custody_proof"])
    assert body["custody_proof"]["covers_nav_as_of"] == feed.iso(cps[-1].as_of)
    entries = body["nav_history"]["checkpoints"]
    assert [e["as_of"] for e in entries] == [feed.iso(c.as_of) for c in reversed(cps)]
    assert [e["custody_proof"]["covers_nav_as_of"] for e in entries] == [e["as_of"] for e in entries]  # each proof backs its own checkpoint
    assert len({e["custody_proof"]["proof_id"] for e in entries}) == 3 and all(e["custody_proof"]["note"] == NOTE for e in entries)
    plain = feed.build(sid, ["nav_history"])["nav_history"]["checkpoints"]
    assert all("custody_proof" not in e for e in plain)  # without the field enabled, history stays as it was


def test_a_checkpoint_published_while_off_has_no_proof_and_says_so(api, fakechain, fac, monkeypatch):
    sid = strategy_with_nav(api, fakechain, -300)  # published while off
    monkeypatch.setattr(settings, "custody_proof_mode", "simulated")
    assert feed.build(sid, ["custody_proof"])["custody_proof"] == {"status": "unavailable", "reason": "no custody proof was generated for this NAV checkpoint"}
    nav_req(api, sid, nav_params(-100))
    body = feed.build(sid, ["nav_history", "custody_proof"])
    assert body["custody_proof"]["mode"] == "simulated"
    old = body["nav_history"]["checkpoints"][-1]
    assert old["custody_proof"]["status"] == "unavailable"  # the earlier NAV is not retroactively "proven"


# ---- never attested, never "verified" ----------------------------------------------------------------------------------------------------------------

def test_a_simulated_proof_is_never_written_into_an_attestation_memo_or_record(api, fakechain, fac, simulated):
    sid = strategy_with_nav(api, fakechain, -120, -60)
    proof = feed.build(sid, ["custody_proof"])["custody_proof"]
    assert fakechain.memos
    for memo in fakechain.memos:
        low = memo.lower()
        for needle in ("simulated", "custody", "zktls", "sim-", proof["proof_id"], proof["proof_hash"], "straight2bank", "claim"):
            assert needle.lower() not in low, (needle, memo)
    with Session(engine) as db:
        for a in db.exec(select(Action).where(Action.strategy_id == sid)).all():
            blob = json.dumps(a.record, default=str).lower()
            assert "simulated" not in blob and "custody" not in blob and proof["proof_id"].lower() not in blob
    attested = feed.build(sid, ["attestations"], now() + timedelta(hours=3))["attestations"]["items"]
    assert any(i["action"] == "publish_nav" for i in attested)  # the NAV itself IS attested, the demo proof is not part of it
    assert "custody" not in json.dumps(attested).lower()


def test_nothing_describes_a_simulated_proof_as_verified(api, fakechain, fac, simulated):
    sid = strategy_with_nav(api, fakechain, -60)
    enable(api, sid)
    paid, _ = pay(api, sid)
    assert paid.status_code == 200
    for body in (paid.json(), api.get(f"/strategies/{sid}/data-feed/preview").json()):
        proof = body["custody_proof"]
        assert proof["mode"] == "simulated" and proof["note"] == NOTE
        assert "verified" not in json.dumps(proof).lower()
    readme = (__import__("pathlib").Path(__file__).resolve().parent.parent / "docs" / "REFERENCE.md").read_text()
    section = readme[readme.index("Custody proofs"):]
    para = section[:section.index("\n\n", 100)]
    assert "simulated" in para.lower() and "not" in para.lower() and "never" in para.lower()
    assert not re.search(r"simulated[^.]*\bis verified\b|verified (bank )?proof", para.lower())


def test_the_verified_mode_is_not_accepted_yet_and_other_values_are_rejected():
    for bad, fragment in (("verified", "not available yet"), ("on", 'must be "off" or "simulated"'), ("", 'must be "off" or "simulated"')):
        with pytest.raises(ValueError, match=fragment):
            Settings(custody_proof_mode=bad, _env_file=None)
    assert Settings(custody_proof_mode=" Simulated ", _env_file=None).custody_proof_mode == "simulated"
    assert Settings(_env_file=None).custody_proof_mode == "off"


def test_a_real_source_can_be_plugged_in_through_the_interface(api, fakechain, fac, monkeypatch):
    class Verified:
        def proof_for(self, strategy_id, as_of, generated_at):
            return {"mode": "verified", "provider": "zkTLS", "custodian": "Standard Chartered (Straight2Bank)", "claim": custody.CLAIM, "claim_holds": True,
                    "covers_nav_as_of": as_of, "proof_id": "zk-1", "proof_hash": "0" * 64, "generated_at": generated_at}

    monkeypatch.setattr(custody, "source", lambda: Verified())
    sid = strategy_with_nav(api, fakechain, -60)
    proof = feed.build(sid, ["custody_proof"])["custody_proof"]
    assert proof["mode"] == "verified" and "note" not in proof  # `note` exists only for simulated proofs


# ---- the feed plumbing ----------------------------------------------------------------------------------------------------------------------------

def test_custody_proof_is_a_sellable_field_and_validated_like_the_others(api, fakechain, fac):
    assert feed.SELLABLE == ("nav_per_share", "nav_history", "hedge_summary", "attestations", "custody_proof")
    assert feed.parse_fields("custody_proof,nav_per_share") == ["nav_per_share", "custody_proof"]  # canonical order
    sid = active(api, fakechain)["id"]
    cfg = enable(api, sid, fields="hedge_summary,custody_proof")
    assert cfg["fields"] == "hedge_summary,custody_proof"
    r = configure(api, sid, {"enabled": "true", "price_usd": "0.01", "pay_to": CUSTOMER, "fields": "custody_proof,balance"})
    assert r.status_code == 400 and "may only contain" in r.json()["error"] and "balance" in r.json()["error"]
    assert enable(api, active(api, fakechain)["id"])["fields"] == ",".join(feed.SELLABLE)  # default: all five


def test_a_buyer_of_only_custody_proof_gets_only_that_and_it_is_unavailable_when_off(api, fakechain, fac):
    sid = strategy_with_nav(api, fakechain, -60)
    enable(api, sid, fields="custody_proof")
    paid, _ = pay(api, sid)
    assert set(paid.json()) == {"strategy_id", "as_of", "custody_proof"} and paid.json()["custody_proof"] == NOT_CONFIGURED


def test_the_preview_is_what_the_buyer_gets_custody_included(api, fakechain, fac, simulated):
    sid = strategy_with_nav(api, fakechain, -60)
    enable(api, sid)
    paid, _ = pay(api, sid)
    prev = api.get(f"/strategies/{sid}/data-feed/preview").json()
    strip = lambda d: {k: v for k, v in d.items() if k != "as_of"}  # noqa: E731
    assert strip(prev) == strip(paid.json()) and prev["custody_proof"]["mode"] == "simulated"


def test_the_never_expose_check_still_holds_with_custody_proofs_in_the_feed(api, fakechain, fac, simulated):
    sid = busy_strategy(api, fakechain)
    nav_req(api, sid, nav_params(-60))
    enable(api, sid)
    body = pay(api, sid)[0].json()
    assert body["custody_proof"]["mode"] == "simulated"
    for path, key, _ in walk(body):
        assert str(key).lower() not in feed.NEVER_EXPOSE, path
