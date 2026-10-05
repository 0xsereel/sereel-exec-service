import base64
import hashlib
import json
import logging
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import base58
import pytest
from solders.keypair import Keypair
from sqlmodel import Session, select

from app import auth
from app.config import settings
from app.db import engine
from app.errors import ServiceError
from app.models import Action, Strategy, UsedNonce, now
from app.state import state
from app.strategies import attest as att
from app.strategies import watcher
from auth_helpers import Signer
from test_strategies import BODY, M, OWNER, SENDER, create, fund, get, rows

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "squads_multisig_devnet.json").read_text())
SQUADS_ADDR = FIXTURE["address"]
# the two members of that real devnet multisig, read independently from the account data
REAL_MEMBERS = ["4ZR4JjQHTnxwnJh9v4vWbJVnX1yCzQ1tqVmKmWjnQqEU", "FTw8UAWgJsfpXgiDe9Kj3KNQpAWYYj9WbNWxCmRHXqzU"]
SID = "strategy-1"
T0 = 1_790_000_000_000


@pytest.fixture(autouse=True)
def clean():
    auth._squads_cache.clear()


# ---- canonical JSON / hash / message ---------------------------------------

@pytest.mark.parametrize("obj,expected", [
    ({}, "{}"), ([], "[]"), (None, "null"), (True, "true"), (False, "false"), (7000, "7000"), (0, "0"), (-5, "-5"),
    ({"b": 1, "a": {"d": 2, "c": [3, {"z": 1, "y": 2}]}}, '{"a":{"c":[3,{"y":2,"z":1}],"d":2},"b":1}'),  # keys sorted recursively
    ("520.5", '"520.5"'),  # a decimal string stays a string
    ("a\"b\\c\n\t\u0001é€", '"a\\"b\\\\c\\n\\t\\u0001é€"'),  # JSON.stringify escapes, non-ASCII left alone
    ({"hedge_ratio_bps": 7000, "target_exposure_units": "520.5"}, '{"hedge_ratio_bps":7000,"target_exposure_units":"520.5"}'),
    ([1, [2, [3]], {"a": [True, None]}], '[1,[2,[3]],{"a":[true,null]}]'),
])
def test_canonical_json_format(obj, expected):
    assert auth.canonical_json(obj) == expected


@pytest.mark.parametrize("bad", [0.6, 520.0, -0.0, 1e21, float("nan"), float("inf"), 2 ** 53, Decimal("1.5"), b"bytes", object()])
def test_canonical_json_refuses_floats_decimals_and_unsafe_integers(bad):
    with pytest.raises(ValueError):
        auth.canonical_json(bad)
    with pytest.raises(ValueError):
        auth.canonical_json({"nested": [bad]})


def test_params_hash_is_lowercase_hex_sha256_of_the_canonical_text():
    assert auth.params_hash({}) == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"  # sha256("{}")
    p = {"amount_usd": "12.5", "destination_wallet_address": "abc"}
    assert auth.params_hash(p) == hashlib.sha256(b'{"amount_usd":"12.5","destination_wallet_address":"abc"}').hexdigest()
    assert auth.params_hash({"b": 1, "a": 2}) == auth.params_hash({"a": 2, "b": 1})  # order of keys does not matter


def test_message_format_is_exactly_the_contract():
    m = auth.build_message("WALLET", "rebalance", "sid-1", {}, "nonce-1", 1759577234123)
    assert m == "sereel-strategy-v1|solana|WALLET|rebalance|sid-1|44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a|nonce-1|1759577234123"


# ---- verification -----------------------------------------------------------

def sign(action="rebalance", params=None, sid=SID, signer=OWNER, ts=T0, **kw):
    params = {} if params is None else params
    return signer.authorization(action, sid, params, ts=ts, **kw)


def check(a, action="rebalance", params=None, sid=SID, at=T0):
    return auth.verify(a, action, sid, {} if params is None else params, now_ms=at)


def test_a_valid_signature_verifies_and_reports_the_signer():
    v = check(sign())
    assert v.signer == OWNER.pubkey and v.timestamp == T0 and uuid.UUID(v.nonce)


def test_missing_authorization_is_authorization_required():
    for missing in (None, {}, ""):
        with pytest.raises(ServiceError) as e:
            check(missing)
        assert (e.value.code, e.value.status) == ("AUTHORIZATION_REQUIRED", 401)


@pytest.mark.parametrize("mutate", [
    lambda a: "not an object",
    lambda a: {k: v for k, v in a.items() if k != "signature"},
    lambda a: {k: v for k, v in a.items() if k != "message"},
    lambda a: {**a, "timestamp": str(a["timestamp"])},  # must be a number, not a string
    lambda a: {**a, "timestamp": True},
    lambda a: {**a, "nonce": "not-a-uuid"},
    lambda a: {**a, "publicKey": "0OIl"},  # not base58
    lambda a: {**a, "publicKey": base58.b58encode(b"short").decode()},  # wrong length
    lambda a: {**a, "signature": 123},
])
def test_malformed_authorization_is_invalid(mutate):
    with pytest.raises(ServiceError) as e:
        check(mutate(sign()))
    assert (e.value.code, e.value.status) == ("AUTHORIZATION_INVALID", 403)


def test_the_signature_binds_action_strategy_and_params():
    a = sign("edit_hedge_settings", {"hedge_ratio_bps": 7000, "target_exposure_units": "520"})
    check(a, "edit_hedge_settings", {"hedge_ratio_bps": 7000, "target_exposure_units": "520"})
    for kw in (dict(action="rebalance", params={}),  # same signature replayed as a different action
               dict(sid="strategy-2"),  # ... on a different strategy
               dict(params={"hedge_ratio_bps": 9999, "target_exposure_units": "520"}),  # ... or with tampered params
               dict(params={"hedge_ratio_bps": 7000})):  # ... or with a field dropped
        args = dict(action="edit_hedge_settings", params={"hedge_ratio_bps": 7000, "target_exposure_units": "520"}, sid=SID)
        args.update(kw)
        with pytest.raises(ServiceError) as e:
            check(sign("edit_hedge_settings", {"hedge_ratio_bps": 7000, "target_exposure_units": "520"}), **args)
        assert e.value.code == "AUTHORIZATION_INVALID", kw


def test_the_clients_message_string_is_never_trusted():
    a = sign()
    other = auth.build_message(OWNER.pubkey, "close_strategy", SID, {}, a["nonce"], T0)
    forged = {**a, "message": other}  # a message for a different action, claimed for this one
    with pytest.raises(ServiceError, match="does not match this request"):
        check(forged)
    # even a perfectly signed message for the wrong network is rejected: we rebuild with network=solana
    with pytest.raises(ServiceError):
        check(OWNER.authorization("rebalance", SID, {}, ts=T0, network="ethereum"))


def test_a_signature_from_another_key_is_rejected():
    a = sign()
    mallory = Signer()
    forged = {**a, "signature": mallory.authorization("rebalance", SID, {}, ts=T0, nonce=a["nonce"])["signature"]}
    with pytest.raises(ServiceError, match="signature is not valid"):
        check(forged)
    claimed = {**mallory.authorization("rebalance", SID, {}, ts=T0), "publicKey": OWNER.pubkey}  # signs, claims the owner's key
    with pytest.raises(ServiceError):
        check(claimed)


@pytest.mark.parametrize("age_ms,ok", [(0, True), (59_000, True), (60_000, True), (60_001, False), (61_000, False)])
def test_age_limit_is_60_seconds(age_ms, ok):
    a = sign(ts=T0 - age_ms)
    if ok:
        check(a)
    else:
        with pytest.raises(ServiceError, match="older than 60s"):
            check(a)


@pytest.mark.parametrize("ahead_ms,ok", [(1_000, True), (29_000, True), (30_000, True), (30_001, False), (31_000, False)])
def test_future_skew_limit_is_30_seconds(ahead_ms, ok):
    a = sign(ts=T0 + ahead_ms)
    if ok:
        check(a)
    else:
        with pytest.raises(ServiceError, match="in the future"):
            check(a)


def test_a_nonce_can_only_be_used_once():
    a = sign()
    check(a)
    with pytest.raises(ServiceError, match="replay"):
        check(a)
    with Session(engine) as db:
        assert len(db.exec(select(UsedNonce)).all()) == 1


def test_a_bad_signature_does_not_burn_the_nonce():
    """Otherwise anybody could invalidate a manager's request by submitting garbage with the nonce in flight."""
    a = sign()
    with pytest.raises(ServiceError):
        check({**a, "signature": base58.b58encode(b"x" * 64).decode()})
    with Session(engine) as db:
        assert db.exec(select(UsedNonce)).all() == []
    check(a)  # the genuine request still works


def test_old_nonces_are_pruned():
    with Session(engine) as db:
        db.add(UsedNonce(nonce="ancient", created_at=now() - timedelta(seconds=auth.NONCE_TTL_S + 10)))
        db.commit()
    check(sign())
    with Session(engine) as db:
        assert "ancient" not in {n.nonce for n in db.exec(select(UsedNonce)).all()}


# ---- decimal strings, never JSON numbers --------------------------------------------------------------------

@pytest.mark.parametrize("params", [
    {"target_exposure_units": 520.5},  # a JSON float where a decimal string belongs
    {"target_exposure_units": 520},  # even a whole JSON number
    {"amount_usd": 12.5},
    {"amount_usd": 0},
    {"hedge_ratio_bps": 7000.0},  # a float where an integer belongs
    {"hedge_ratio_bps": "7000"},  # a string where an integer belongs
    {"hedge_ratio_bps": True},
    {"hedge_ratio_bps": -1},
])
def test_json_numbers_are_rejected_where_decimal_strings_belong(params):
    a = sign("edit_hedge_settings", {"target_exposure_units": "1"})  # the signature itself is fine; the params are not
    with pytest.raises(ServiceError) as e:
        auth.verify(a, "edit_hedge_settings", SID, params, now_ms=T0)
    assert (e.value.code, e.value.status) == ("AUTHORIZATION_INVALID", 403)


def test_the_rejection_says_what_to_send_instead():
    with pytest.raises(ServiceError) as e:
        auth.verify(sign(), "rebalance", SID, {"amount_usd": 12.5}, now_ms=T0)
    assert 'decimal string such as "520.5"' in e.value.message and "not a JSON number" in e.value.message and "params.amount_usd" in e.value.message


@pytest.mark.parametrize("good", ["0", "1", "520", "520.5", "0.000001", "1250.75", "123456789.123456789012345678"])
def test_valid_decimal_strings_are_accepted(good):
    auth.validate_params({"amount_usd": good})
    auth.validate_params({"target_exposure_units": good})


@pytest.mark.parametrize("bad", ["", " 1", "1 ", "+1", "-1", "1.", ".5", "01", "00.5", "1e3", "1E3", "1,5", "0x10", "NaN", "Infinity",
                                 "1.1234567890123456789", "١٢٣", None, [], {}])
def test_malformed_decimal_strings_are_rejected(bad):
    with pytest.raises(ServiceError) as e:
        auth.validate_params({"amount_usd": bad})
    assert e.value.code == "AUTHORIZATION_INVALID"


def test_other_param_types_and_unknown_fields():
    auth.validate_params({"destination_wallet_address": "abc", "owner_pubkey": "x", "owner_multisig": "y", "hedge_ratio_bps": 0})
    for bad in ({"destination_wallet_address": 5}, {"owner_pubkey": None}, {"surprise": "x"}):
        with pytest.raises(ServiceError):
            auth.validate_params(bad)


def test_a_number_is_rejected_before_anything_is_stored_or_checked(api):
    """Rejected params never burn a nonce and never reach the owner check."""
    a = sign("edit_hedge_settings", {"target_exposure_units": "1"})
    with pytest.raises(ServiceError):
        auth.verify(a, "edit_hedge_settings", SID, {"target_exposure_units": 1}, now_ms=T0)
    with Session(engine) as db:
        assert db.exec(select(UsedNonce)).all() == []


def test_published_test_vectors_match_the_implementation_and_the_readme():
    """The README tells client authors to check their code against these exact values."""
    v = json.loads((Path(__file__).parent / "fixtures" / "auth_vectors.json").read_text())
    signer = Signer(bytes.fromhex(v["seed_hex"]))
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    assert signer.pubkey == v["publicKey"] and v["publicKey"] in readme
    for vec in v["vectors"]:
        assert auth.canonical_json(vec["params"]) == vec["canonical"]
        assert auth.params_hash(vec["params"]) == vec["params_hash"]
        a = signer.authorization(vec["action"], v["strategy_id"], vec["params"], ts=v["timestamp"], nonce=v["nonce"])
        assert (a["message"], a["signature"]) == (vec["message"], vec["signature"])  # ed25519 is deterministic
        for field in ("canonical", "params_hash", "message", "signature"):
            assert vec[field] in readme, f"README is missing the {field} of the {vec['action']} vector"
        verified = auth.verify(a, vec["action"], v["strategy_id"], vec["params"], now_ms=v["timestamp"])
        assert verified.signer == signer.pubkey
        with Session(engine) as db:  # the vectors reuse one nonce; clear it so each can be verified
            db.exec(__import__("sqlmodel").delete(UsedNonce))
            db.commit()


# ---- Squads ----------------------------------------------------------------

def squads_account(members, owner=None, perms=7, padding=40, option_some=False):
    raw = (auth.SQUADS_DISCRIMINATOR + bytes(32) + bytes(32) + (1).to_bytes(2, "little") + (0).to_bytes(4, "little")
           + (5).to_bytes(8, "little") + (0).to_bytes(8, "little") + (b"\x01" + bytes(32) if option_some else b"\x00") + b"\xff"
           + len(members).to_bytes(4, "little") + b"".join(base58.b58decode(m) + bytes([perms]) for m in members) + bytes(padding))
    return {"owner": owner or settings.squads_program_id, "data": [base64.b64encode(raw).decode(), "base64"]}


def test_the_real_devnet_squads_account_parses():
    raw = base64.b64decode(FIXTURE["data_b64"])
    assert FIXTURE["owner"] == settings.squads_program_id
    members = auth.parse_squads_multisig(raw)
    assert len(members) == 2 and all(len(base58.b58decode(m)) == 32 for m in members)
    assert [m[:6] for m in members] == ["4ZR4Jj", "FTw8UA"]


def test_parser_handles_both_rent_collector_layouts_and_rejects_garbage():
    a, b = Signer().pubkey, Signer().pubkey
    for some in (False, True):
        raw = base64.b64decode(squads_account([a, b], option_some=some)["data"][0])
        assert auth.parse_squads_multisig(raw) == [a, b]
    with pytest.raises(ValueError):
        auth.parse_squads_multisig(b"\x00" * 8 + bytes(200))  # wrong discriminator
    good = base64.b64decode(squads_account([a])["data"][0])
    with pytest.raises(ValueError):
        auth.parse_squads_multisig(good[:90])  # truncated
    huge = bytearray(good)
    off = 8 + 32 + 32 + 2 + 4 + 8 + 8 + 1 + 1
    huge[off:off + 4] = (60_000).to_bytes(4, "little")  # claims 60k members that are not there
    with pytest.raises(ValueError):
        auth.parse_squads_multisig(bytes(huge))


def test_squads_members_reads_caches_and_refreshes(fakechain):
    a, b = Signer().pubkey, Signer().pubkey
    fakechain.accounts["MS"] = squads_account([a, b])
    assert auth.squads_members("MS") == [a, b] and auth.squads_members("MS") == [a, b]
    assert fakechain.rpc_calls.count("getAccountInfo") == 1  # second call served from the cache
    fakechain.accounts["MS"] = squads_account([a])  # a member is removed on-chain
    assert auth.squads_members("MS") == [a, b]  # still cached: that is the (short) staleness window
    assert auth.squads_members("MS", fresh=True) == [a]  # a fresh read sees it
    auth._squads_cache["MS"] = (time.time() - settings.squads_cache_s - 1, [a, b])
    assert auth.squads_members("MS") == [a]  # and an expired entry is re-read


def test_squads_members_rejects_non_squads_accounts_and_reports_rpc_failures(fakechain):
    fakechain.accounts["WALLET"] = {"owner": "11111111111111111111111111111111", "data": ["", "base64"]}
    fakechain.accounts["WRONGPROG"] = squads_account([Signer().pubkey], owner="11111111111111111111111111111111")
    fakechain.accounts["JUNK"] = {"owner": settings.squads_program_id, "data": [base64.b64encode(b"\x00" * 100).decode(), "base64"]}
    for addr, fragment in (("WALLET", "not owned by the Squads program"), ("WRONGPROG", "not owned by the Squads program"),
                           ("JUNK", "not a Squads multisig account"), ("NOPE", "not an account")):
        with pytest.raises(ServiceError, match=fragment) as e:
            auth.squads_members(addr)
        assert e.value.code == "BAD_REQUEST"
    fakechain.accounts["__down__"] = True
    with pytest.raises(ServiceError) as e:
        auth.squads_members("MS2")
    assert (e.value.code, e.value.status) == ("CHAIN_UNAVAILABLE", 503)


def test_removed_members_lose_access_once_the_cache_expires(fakechain):
    a, b = Signer(), Signer()
    st = Strategy(fund_id="f", market_id=M, hedge_ratio_bps=1, leverage=1, rebalance_band_bps=1, return_wallet_address="w",
                  registered_sender_address="s", expires_at=now(), owner_multisig="MS")
    fakechain.accounts["MS"] = squads_account([a.pubkey, b.pubkey])
    auth.assert_signer_owns(st, b.pubkey)
    fakechain.accounts["MS"] = squads_account([a.pubkey])  # b is removed on-chain
    auth._squads_cache["MS"] = (time.time() - settings.squads_cache_s - 1, [a.pubkey, b.pubkey])
    with pytest.raises(ServiceError, match="not a current member"):
        auth.assert_signer_owns(st, b.pubkey)


# ---- ownership ---------------------------------------------------------------

def test_only_the_bound_owner_may_sign():
    st = Strategy(fund_id="f", market_id=M, hedge_ratio_bps=1, leverage=1, rebalance_band_bps=1, return_wallet_address="w",
                  registered_sender_address="s", expires_at=now(), owner_pubkey=OWNER.pubkey)
    auth.assert_signer_owns(st, OWNER.pubkey)
    with pytest.raises(ServiceError, match="not the strategy's owner") as e:
        auth.assert_signer_owns(st, Signer().pubkey)
    assert (e.value.code, e.value.status) == ("AUTHORIZATION_INVALID", 403)


def test_a_strategy_with_no_owner_cannot_be_authorised_by_any_signature():
    st = Strategy(fund_id="f", market_id=M, hedge_ratio_bps=1, leverage=1, rebalance_band_bps=1, return_wallet_address="w",
                  registered_sender_address="s", expires_at=now())
    with pytest.raises(ServiceError, match="no owner bound"):
        auth.assert_signer_owns(st, OWNER.pubkey)


# ---- creation binds the owner ------------------------------------------------

def test_creation_requires_exactly_one_owner(api, fakechain):
    fakechain.accounts[SQUADS_ADDR] = {"owner": FIXTURE["owner"], "data": [FIXTURE["data_b64"], "base64"]}
    neither = {k: v for k, v in BODY.items() if k != "owner_pubkey"}
    for body, fragment in ((neither, "owner_pubkey or owner_multisig is required"),
                           ({**BODY, "owner_multisig": SQUADS_ADDR}, "not both"),
                           ({**BODY, "owner_pubkey": "nope"}, "owner_pubkey 'nope'"),
                           ({**BODY, "owner_pubkey": None, "owner_multisig": "nope"}, "owner_multisig 'nope'")):
        r = api.post("/strategies", json=body)
        assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and fragment in r.json()["error"], body
    assert rows(Strategy) == []


def test_an_owner_multisig_must_be_a_real_squads_account(api, fakechain):
    r = api.post("/strategies", json={**BODY, "owner_pubkey": None, "owner_multisig": str(Keypair().pubkey())})
    assert r.status_code == 400 and "not an account" in r.json()["error"]
    fakechain.accounts[SQUADS_ADDR] = {"owner": FIXTURE["owner"], "data": [FIXTURE["data_b64"], "base64"]}
    s = api.post("/strategies", json={**BODY, "owner_pubkey": None, "owner_multisig": SQUADS_ADDR}).json()
    assert s["owner_multisig"] == SQUADS_ADDR and s["owner_pubkey"] is None


def test_the_owner_is_stored_exposed_and_in_the_creation_and_deploy_records(api, fakechain):
    s = create(api)
    assert s["owner_pubkey"] == OWNER.pubkey and s["owner_multisig"] is None
    (created,) = [a for a in rows(Action) if a.action == "create"]
    assert created.record["owner_pubkey"] == OWNER.pubkey
    fund(api, fakechain, s)
    (deploy,) = [a for a in rows(Action) if a.action == "deploy"]
    assert deploy.record["owner_pubkey"] == OWNER.pubkey and deploy.record["owner_multisig"] is None
    memo = json.loads(fakechain.memos[-1])
    assert memo["h"] == att.record_hash(deploy.record)  # the attested hash commits to the owner too


# ---- changing the owner (a signed, attested action) -------------------------------

def change(api, sid, params, signer=OWNER, **kw):
    body = {**params, "authorization": signer.authorization("change_owner", sid, params, **kw)}
    return api.post(f"/strategies/{sid}/owner", json=body), body


def test_the_current_owner_can_hand_over_and_it_is_attested(api, fakechain):
    s = create(api)
    new = Signer()
    r, _ = change(api, s["id"], {"owner_pubkey": new.pubkey})
    assert r.status_code == 200 and r.json()["owner_pubkey"] == new.pubkey
    (act,) = [a for a in rows(Action) if a.action == "change_owner"]
    assert act.record["from"] == {"owner_pubkey": OWNER.pubkey, "owner_multisig": None}
    assert act.record["to"]["owner_pubkey"] == new.pubkey and act.record["signed_by"] == OWNER.pubkey
    m = json.loads(fakechain.memos[-1])
    assert m["a"] == "change_owner" and m["id"] == s["id"] and m["h"] == att.record_hash(act.record) and act.attestation_sig
    # from now on only the NEW owner counts
    old_again, _ = change(api, s["id"], {"owner_pubkey": OWNER.pubkey})
    assert old_again.status_code == 403 and old_again.json()["code"] == "AUTHORIZATION_INVALID"
    back, _ = change(api, s["id"], {"owner_pubkey": OWNER.pubkey}, signer=new)
    assert back.status_code == 200 and back.json()["owner_pubkey"] == OWNER.pubkey


def test_change_owner_needs_a_valid_signature_from_the_owner(api):
    s = create(api)
    params = {"owner_pubkey": Signer().pubkey}
    r = api.post(f"/strategies/{s['id']}/owner", json=params)  # no authorization at all
    assert (r.status_code, r.json()["code"]) == (401, "AUTHORIZATION_REQUIRED") and set(r.json()) == {"error", "code"}
    r, _ = change(api, s["id"], params, signer=Signer())  # validly signed, but by a stranger
    assert (r.status_code, r.json()["code"]) == (403, "AUTHORIZATION_INVALID") and "not the strategy's owner" in r.json()["error"]
    r, body = change(api, s["id"], params, ts=int(time.time() * 1000) - 120_000)  # too old
    assert r.status_code == 403 and "older than 60s" in r.json()["error"]
    body = {"owner_pubkey": Signer().pubkey, "authorization": OWNER.authorization("change_owner", s["id"], params)}
    assert api.post(f"/strategies/{s['id']}/owner", json=body).status_code == 403  # params in the body differ from what was signed
    assert get(api, s["id"])["owner_pubkey"] == OWNER.pubkey  # nothing changed


def test_a_captured_authorization_cannot_be_replayed(api):
    s = create(api)
    new = Signer()
    r, body = change(api, s["id"], {"owner_pubkey": new.pubkey})
    assert r.status_code == 200
    again = api.post(f"/strategies/{s['id']}/owner", json=body)
    assert again.status_code == 403 and "replay" in again.json()["error"]


def test_change_owner_input_validation_and_state(api, fakechain):
    s = create(api)
    for params, fragment in (({}, "owner_pubkey or owner_multisig is required"),
                             ({"owner_pubkey": "nope"}, "not a valid Solana address"),
                             ({"owner_pubkey": Signer().pubkey, "owner_multisig": SQUADS_ADDR}, "not both"),
                             ({"owner_multisig": str(Keypair().pubkey())}, "not an account")):
        r, _ = change(api, s["id"], params)
        assert r.status_code == 400 and fragment in r.json()["error"], params
    assert api.post(f"/strategies/{s['id']}/cancel").json()["status"] == "cancelled"
    r, _ = change(api, s["id"], {"owner_pubkey": Signer().pubkey})
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"
    assert api.post("/strategies/nope/owner", json={"owner_pubkey": Signer().pubkey}).status_code == 404
    with Session(engine) as db:
        assert db.exec(select(UsedNonce)).all() == []  # a rejected or unknown-target request never consumed a nonce


def test_a_multisig_owned_strategy_is_managed_by_its_current_members(api, fakechain):
    member, outsider = Signer(), Signer()
    ms = str(Keypair().pubkey())  # a Squads multisig ACCOUNT address (a valid pubkey)
    fakechain.accounts[ms] = squads_account([member.pubkey, Signer().pubkey])
    s = api.post("/strategies", json={**BODY, "owner_pubkey": None, "owner_multisig": ms}).json()
    assert s["owner_multisig"] == ms
    r, _ = change(api, s["id"], {"owner_pubkey": outsider.pubkey}, signer=outsider)
    assert r.status_code == 403 and "not a current member" in r.json()["error"]
    r, _ = change(api, s["id"], {"owner_pubkey": member.pubkey}, signer=member)  # a member hands it to a single wallet
    assert r.status_code == 200 and r.json()["owner_pubkey"] == member.pubkey and r.json()["owner_multisig"] is None
    (act,) = [a for a in rows(Action) if a.action == "change_owner"]
    assert act.record["from"] == {"owner_pubkey": None, "owner_multisig": ms} and act.record["signed_by"] == member.pubkey


# ---- DEV_AUTH_BYPASS -----------------------------------------------------------

def test_without_the_bypass_nothing_is_open_by_default(api):
    assert settings.dev_auth_bypass is False and settings.auth_bypass_active is False
    s = create(api)
    assert api.post(f"/strategies/{s['id']}/owner", json={"owner_pubkey": Signer().pubkey}).status_code == 401


def test_the_bypass_skips_auth_and_shouts_on_every_request(api, monkeypatch, caplog):
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "allow_mainnet", False)
    s = create(api)
    caplog.set_level(logging.WARNING, logger="sereel.auth")
    for _ in range(2):
        r = api.post(f"/strategies/{s['id']}/owner", json={"owner_pubkey": Signer().pubkey})  # no authorization
        assert r.status_code == 200
    loud = [r for r in caplog.records if "DEV_AUTH_BYPASS" in r.getMessage()]
    assert len(loud) == 2 and all(r.levelno == logging.WARNING and s["id"] in r.getMessage() for r in loud)
    (act,) = [a for a in rows(Action) if a.action == "change_owner"][-1:]
    assert act.record["signed_by"] == "dev-bypass"  # the record says so


def test_the_bypass_lets_a_strategy_be_created_without_an_owner_but_only_in_bypass(api, monkeypatch):
    neither = {k: v for k, v in BODY.items() if k != "owner_pubkey"}
    assert api.post("/strategies", json=neither).status_code == 400
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    assert api.post("/strategies", json=neither).status_code == 200


def test_the_bypass_is_inert_when_mainnet_is_allowed(api, monkeypatch):
    s = create(api)
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "allow_mainnet", True)
    assert settings.auth_bypass_active is False
    assert api.post(f"/strategies/{s['id']}/owner", json={"owner_pubkey": Signer().pubkey}).status_code == 401


def test_startup_refuses_the_bypass_with_mainnet_and_warns_loudly_otherwise(monkeypatch, fakechain, caplog):
    from fastapi.testclient import TestClient

    from app import main
    from app.payments import scheduler

    class NoSched:
        def shutdown(self, wait=False):
            pass

    monkeypatch.setattr(scheduler, "start", lambda: NoSched())
    monkeypatch.setattr(watcher, "register", lambda s: None)
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "allow_mainnet", True)
    with pytest.raises(RuntimeError, match="DEV_AUTH_BYPASS=true is not allowed together with ALLOW_MAINNET=true"):
        with TestClient(main.app):
            pass
    monkeypatch.setattr(settings, "allow_mainnet", False)
    caplog.set_level(logging.WARNING, logger="sereel")
    with TestClient(main.app):
        pass
    assert any("DEV_AUTH_BYPASS IS ON" in r.getMessage() for r in caplog.records)
