"""Cantina's proxy called POST /strategies/intents and got 405: the path read as `/strategies/{id}` with id='intents', which has
GET/PATCH/DELETE but no POST. The v4 path is POST /strategies; the old plan's /intents is accepted as an alias."""
from app.models import Strategy
from test_strategies import BODY, rows


def test_the_canonical_path_still_works(api):
    r = api.post("/strategies", json=BODY)
    assert r.status_code == 200 and r.json()["status"] == "pending_funding"


def test_the_intents_alias_creates_the_same_thing(api):
    a = api.post("/strategies", json=BODY).json()
    r = api.post("/strategies/intents", json=BODY)
    assert r.status_code == 200, r.text  # not 405
    b = r.json()
    assert set(b) == set(a) and b["status"] == "pending_funding" and b["intent_id"] and b["id"] != a["id"]
    assert b["funding_address"] == a["funding_address"] and b["owner_pubkey"] == a["owner_pubkey"]
    assert len(rows(Strategy)) == 2


def test_the_alias_has_every_rule_of_the_canonical_path(api):
    no_owner = {k: v for k, v in BODY.items() if k != "owner_pubkey"}
    r = api.post("/strategies/intents", json=no_owner)
    assert r.status_code == 400 and "owner_pubkey or owner_multisig is required" in r.json()["error"] and set(r.json()) == {"error", "code"}
    assert api.post("/strategies/intents", json={**BODY, "leverage": 9}).status_code == 400
    assert api.post("/strategies/intents", json=BODY, headers={"X-Sereel-Key": "wrong"}).status_code == 401  # still authenticated
    assert api.post("/strategies/intents", json={}).status_code == 400


def test_the_alias_honours_the_org_and_user_headers(api):
    r = api.post("/strategies/intents", json=BODY, headers={"X-Sereel-Org": "org-a", "X-Sereel-User": "u1"})
    assert r.status_code == 200 and r.json()["owner_user_id"] == "u1"
    assert api.get(f"/strategies/{r.json()['id']}", headers={"X-Sereel-Org": "org-b"}).status_code == 404


def test_the_alias_does_not_change_how_other_methods_on_that_path_behave(api):
    """GET /strategies/intents is still 'strategy id = intents': a 404, not a list and not a 405."""
    r = api.get("/strategies/intents")
    assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
    assert api.get("/strategies/funding-address").status_code == 200  # the static route is untouched
