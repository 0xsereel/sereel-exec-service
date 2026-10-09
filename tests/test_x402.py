"""Step 5: the opt-in x402 data feed, direct-to-customer settlement, the allow-list of what is sold, and NAV publishing. A fake facilitator
stands in for x402.org; the wire format itself was checked live against the real /verify."""
import base64
import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from sqlmodel import Session, select
from typer.testing import CliRunner

from app import delegates
from app import solana_client as sol
from app.ai import decisions
from app.config import settings
from app.db import engine
from app.errors import CODES, ServiceError
from app.models import Action, DataFeed, DataPayment, NavCheckpoint, UsedNonce, now
from app.state import state
from app.strategies import attest as att
from app.x402 import client, facilitator, feed, requirements
from app.x402 import router as x402_router
from auth_helpers import Signer
from cli.sereel_cli import app as cli_app
from test_delegation import DELEGATE, grant, grant_params
from test_step1 import M, OWNER, SENDER, active, get, patch, rebalance
from test_strategies import BODY

D = Decimal
FEE_PAYER = "CKPKJWNdJEqa81x7CkZ14BVPiY6y16Sxs7owznqtWYp5"
CUSTOMER = str(Keypair().pubkey())
BLOCKHASH = str(Hash.default())
KEYLESS = {"X-Sereel-Key": ""}  # the public route must work without the service key
runner = CliRunner()


class FakeFacilitator:
    def __init__(self):
        self.calls, self.verify_result, self.settle_result = [], None, None
        self.supported_error = None
        self.n = 0
        self.verify_result = {"isValid": True, "payer": "BuyerWallet11111111111111111111111111111111"}

    def settle_default(self):
        self.n += 1
        return {"success": True, "payer": "BuyerWallet11111111111111111111111111111111", "transaction": f"SETTLEMENT{self.n}", "network": settings.x402_network}

    def __call__(self, path, body=None):
        self.calls.append((path, body))
        if path == "/supported":
            if self.supported_error:
                raise self.supported_error
            return {"kinds": [{"x402Version": 2, "scheme": "exact", "network": settings.x402_network, "extra": {"feePayer": FEE_PAYER}}]}
        result = self.verify_result if path == "/verify" else (self.settle_result if self.settle_result is not None else None)
        if isinstance(result, Exception):
            raise result
        return result if result is not None else self.settle_default()

    def paths(self):
        return [p for p, _ in self.calls]


@pytest.fixture
def fac(api, monkeypatch):
    f = FakeFacilitator()
    monkeypatch.setattr(facilitator, "call", f)
    facilitator._supported.update(at=0.0, fee_payer=None)
    x402_router._hits.clear()
    x402_router._seen.clear()
    monkeypatch.setattr(settings, "public_url", "https://feed.test")
    created = []
    monkeypatch.setattr(requirements, "ensure_token_account", lambda owner: created.append(owner) or ("ATA-" + owner[:6], None))
    f.created = created
    return f


def nonces():
    with Session(engine) as db:
        return len(db.exec(select(UsedNonce)).all())


def configure(api, sid, params, signer=OWNER, **kw):
    return api.post(f"/strategies/{sid}/data-feed", json={**params, "authorization": signer.authorization("configure_data_feed", sid, params, **kw)})


def enable(api, sid, price="0.01", pay_to=CUSTOMER, fields=None, **extra):
    params = {"enabled": "true", "price_usd": price, "pay_to": pay_to, **({"fields": fields} if fields else {}), **extra}
    r = configure(api, sid, params)
    assert r.status_code == 200, r.text
    return r.json()


def feed_url(sid):
    return f"/x402/strategies/{sid}"


def required_of(r):
    return json.loads(base64.b64decode(r.headers["PAYMENT-REQUIRED"]))


def pay(api, sid, payer=None, tweak=None, tx=None):
    """Make the buyer's request: fetch the 402, build a real payment, optionally tamper with what the buyer claims to have signed."""
    first = api.get(feed_url(sid), headers=KEYLESS)
    required = required_of(first)
    payer = payer or Keypair()
    payment = json.loads(base64.b64decode(client.payment_header(payer, required, BLOCKHASH)))
    if tx is not None:
        payment["payload"]["transaction"] = tx
    if tweak:
        tweak(payment)
    header = base64.b64encode(json.dumps(payment).encode()).decode()
    return api.get(feed_url(sid), headers={**KEYLESS, "PAYMENT-SIGNATURE": header}), payment


@pytest.fixture
def strat(api, fakechain, fac):
    s = active(api, fakechain)
    return s["id"]


# ---- opt-in: nothing is exposed until the owner enables it -------------------------------------------------------------------------------------------

def test_a_new_strategy_exposes_nothing_and_unknown_and_disabled_answer_identically(api, strat, fac):
    r = api.get(feed_url(strat), headers=KEYLESS)
    unknown = api.get(feed_url("no-such-strategy"), headers=KEYLESS)
    assert r.status_code == unknown.status_code == 404 and r.json() == unknown.json() == {"error": "no data feed is available for this id", "code": "DATA_FEED_DISABLED"}
    assert "PAYMENT-REQUIRED" not in r.headers and fac.calls == []  # not even a hint of a price
    cfg = api.get(f"/strategies/{strat}/data-feed").json()
    assert set(cfg) == {"enabled", "price_usd", "pay_to", "fields", "endpoint_url", "total_income_usd", "payment_count", "last_paid_at", "usdc_mint"}
    assert cfg["enabled"] is False and cfg["price_usd"] is None and cfg["pay_to"] is None and cfg["fields"] is None and cfg["payment_count"] == 0
    assert cfg["total_income_usd"] == "0.00" and cfg["last_paid_at"] is None and cfg["usdc_mint"] == settings.x402_usdc_mint
    assert cfg["endpoint_url"] == f"https://feed.test/x402/strategies/{strat}"
    row = get(api, strat)
    assert row["data_feed_enabled"] is False and row["data_income_usd"] == "0.00"
    assert api.get(f"/strategies/{strat}/data-feed/preview").json()["code"] == "DATA_FEED_DISABLED"


def test_the_owner_enables_the_feed_and_it_is_attested(api, strat, fac, fakechain):
    cfg = enable(api, strat, fields="attestations,nav_per_share")
    assert cfg["enabled"] is True and cfg["price_usd"] == "0.01" and cfg["pay_to"] == CUSTOMER
    assert cfg["fields"] == "nav_per_share,attestations"  # canonical order, comma-separated string
    assert fac.created == [CUSTOMER]  # the customer's USDC token account was ensured first
    with Session(engine) as db:
        a = db.exec(select(Action).where(Action.action == "configure_data_feed")).one()
    assert a.record["enabled"] is True and a.record["pay_to"] == CUSTOMER and a.record["signed_by"] == OWNER.pubkey and a.record["token_account"]
    assert json.loads(fakechain.memos[-1])["a"] == "configure_data_feed" and a.attestation_sig
    assert get(api, strat)["data_feed_enabled"] is True


def test_pay_to_defaults_to_the_owners_wallet_and_fields_to_all_four(api, strat, fac):
    r = configure(api, strat, {"enabled": "true", "price_usd": "0.05"})
    assert r.status_code == 200 and r.json()["pay_to"] == OWNER.pubkey and r.json()["fields"] == ",".join(feed.SELLABLE)


@pytest.mark.parametrize("over,fragment", [
    ({"price_usd": "0"}, "above 0"), ({"price_usd": "1000.01"}, "at most 1000"), ({"price_usd": "0.0000001"}, "at most 6 decimals"),
    ({"fields": "nav_per_share,signals"}, "may only contain"), ({"fields": "position"}, "may only contain"), ({"fields": "nav_per_share,nav_per_share"}, "twice"),
    ({"fields": "nav_per_share,"}, "comma-separated"), ({"pay_to": "not-an-address"}, "valid Solana address"), ({"enabled": "yes"}, '"true" or "false"'),
    ({"fund_address": "nope"}, "valid Solana address"),
])
def test_bad_settings_are_refused_before_the_owners_signature_is_spent_and_enable_nothing(api, strat, fac, over, fragment):
    used = nonces()
    p = {"enabled": "true", "price_usd": "0.01", "pay_to": CUSTOMER, **over}
    r = configure(api, strat, p)
    assert r.status_code == 400 and r.json()["code"] == "BAD_REQUEST" and fragment in r.json()["error"] and set(r.json()) == {"error", "code"}
    assert nonces() == used and feed.config(strat) is None and fac.created == []


def test_enabling_needs_a_price_and_a_multisig_owner_needs_an_explicit_pay_to(api, strat, fac):
    assert "price_usd is required" in configure(api, strat, {"enabled": "true"}).json()["error"]
    with Session(engine) as db:
        from app.models import Strategy
        st = db.get(Strategy, strat)
        st.owner_pubkey, st.owner_multisig = None, str(Keypair().pubkey())
        db.add(st)
        db.commit()
    r = configure(api, strat, {"enabled": "true", "price_usd": "0.01"})
    assert r.status_code == 400 and "multisig" in r.json()["error"]


def test_numbers_in_the_signed_params_are_refused(api, strat, fac):
    p = {"enabled": "true", "price_usd": "0.01", "pay_to": CUSTOMER}
    body = {**p, "price_usd": 0.01, "authorization": OWNER.authorization("configure_data_feed", strat, p)}
    r = api.post(f"/strategies/{strat}/data-feed", json=body)
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"


def test_only_the_owner_can_configure_not_a_stranger_not_a_delegate(api, strat, fac):
    p = {"enabled": "true", "price_usd": "0.01", "pay_to": CUSTOMER}
    assert api.post(f"/strategies/{strat}/data-feed", json=p).json()["code"] == "AUTHORIZATION_REQUIRED"
    assert configure(api, strat, p, signer=Signer()).json()["code"] == "AUTHORIZATION_INVALID"
    grant(api, strat, grant_params(rebalance_within_band_only="false"))
    r = configure(api, strat, p, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_NOT_ALLOWED"
    assert feed.config(strat) is None and fac.created == []  # nothing enabled, no token account paid for


def test_a_token_account_that_cannot_be_created_refuses_the_enable_and_names_the_funding_sol(api, strat, fac, monkeypatch):
    monkeypatch.undo()  # drop the stub from `fac`, keep the real function
    monkeypatch.setattr(facilitator, "call", fac)
    monkeypatch.setattr(settings, "public_url", "https://feed.test")
    monkeypatch.setattr(sol, "rpc", lambda *a, **k: {"value": None})
    monkeypatch.setattr(sol, "funding_kp", lambda: Keypair())
    monkeypatch.setattr(sol, "sol_balance", lambda pk: D("0.0004"))

    def broke(*a, **k):
        raise sol.SolanaError("insufficient lamports")
    monkeypatch.setattr(sol, "send", broke)
    r = configure(api, strat, {"enabled": "true", "price_usd": "0.01", "pay_to": CUSTOMER})
    assert r.status_code == 503 and r.json()["code"] == "CHAIN_UNAVAILABLE" and "0.0004 SOL" in r.json()["error"] and "Nothing was enabled" in r.json()["error"]
    assert feed.config(strat) is None  # a feed is never live with a pay_to that cannot be settled to
    assert api.get(feed_url(strat), headers=KEYLESS).status_code == 404


def test_ensure_token_account_is_idempotent_and_creates_only_when_missing(monkeypatch):
    sent = []
    monkeypatch.setattr(sol, "funding_kp", lambda: Keypair())
    monkeypatch.setattr(sol, "send", lambda ixs, signers, **k: sent.append(ixs) or "SIG")
    monkeypatch.setattr(sol, "rpc", lambda *a, **k: {"value": {"lamports": 1}})
    addr, created = requirements.ensure_token_account(CUSTOMER)
    assert created is None and sent == [] and addr == str(requirements.x402_ata(CUSTOMER))
    monkeypatch.setattr(sol, "rpc", lambda *a, **k: {"value": None})
    addr, created = requirements.ensure_token_account(CUSTOMER)
    assert created == "SIG" and len(sent) == 1 and len(sent[0]) == 1
    # an off-curve owner (a multisig vault PDA) works too
    from solders.pubkey import Pubkey
    vault, _ = Pubkey.find_program_address([b"vault"], Pubkey.from_string(settings.squads_program_id))
    assert requirements.ensure_token_account(str(vault))[1] == "SIG"


def test_disabling_stops_sales_at_once_and_keeps_the_settings(api, strat, fac):
    enable(api, strat)
    assert api.get(feed_url(strat), headers=KEYLESS).status_code == 402
    r = configure(api, strat, {"enabled": "false"})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["price_usd"] == "0.01" and r.json()["pay_to"] == CUSTOMER
    assert api.get(feed_url(strat), headers=KEYLESS).status_code == 404
    back = configure(api, strat, {"enabled": "true"})  # re-enabling reuses the stored price and wallet
    assert back.status_code == 200 and back.json()["enabled"] is True and back.json()["price_usd"] == "0.01"


# ---- the 402 and the payment ----------------------------------------------------------------------------------------------------------------------

def test_unpaid_requests_get_a_402_with_our_terms_and_need_no_service_key(api, strat, fac):
    enable(api, strat, price="0.0125", pay_to=CUSTOMER)
    r = api.get(feed_url(strat), headers=KEYLESS)
    assert r.status_code == 402 and r.json() == {}  # the v2 transport: an empty body, the terms are in the header
    req = required_of(r)
    assert req["x402Version"] == 2 and req["resource"]["url"] == f"https://feed.test/x402/strategies/{strat}" and req["resource"]["mimeType"] == "application/json"
    assert req["accepts"] == [{"scheme": "exact", "network": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1", "amount": "12500",
                               "asset": "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU", "payTo": CUSTOMER, "maxTimeoutSeconds": 60, "extra": {"feePayer": FEE_PAYER}}]
    assert fac.paths() == ["/supported"]  # nothing was verified or settled for an unpaid request
    configure(api, strat, {"enabled": "true", "price_usd": "0.02", "pay_to": str(Keypair().pubkey())})
    assert required_of(api.get(feed_url(strat), headers=KEYLESS))["accepts"][0]["amount"] == "20000"  # follows the stored config


def test_the_circle_mint_is_for_x402_only_and_never_the_services_own_mock_mint():
    assert settings.x402_usdc_mint == "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU" and settings.x402_usdc_mint != settings.stablecoin_mint


def test_a_valid_payment_settles_to_the_customer_returns_the_enabled_fields_and_records_income(api, strat, fac):
    enable(api, strat, fields="hedge_summary,attestations")
    r, payment = pay(api, strat)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"strategy_id", "as_of", "hedge_summary", "attestations"}  # only what is enabled
    settlement = json.loads(base64.b64decode(r.headers["PAYMENT-RESPONSE"]))
    assert settlement["success"] is True and settlement["transaction"] == "SETTLEMENT1"
    paths = fac.paths()
    assert paths[-2:] == ["/verify", "/settle"]
    sent = dict(fac.calls)["/verify"]
    assert sent["x402Version"] == 2 and sent["paymentRequirements"]["payTo"] == CUSTOMER and sent["paymentRequirements"]["amount"] == "10000"
    row = api.get(f"/strategies/{strat}/data-feed").json()
    assert (row["total_income_usd"], row["payment_count"]) == ("0.01", 1) and row["last_paid_at"].endswith("Z")
    assert api.get(f"/strategies/{strat}/data-feed/payments").json() == [{
        "payer": "BuyerWallet11111111111111111111111111111111", "amount_usd": "0.01", "mint": settings.x402_usdc_mint, "tx_signature": "SETTLEMENT1",
        "settled_at": row["last_paid_at"], "fields_served": "hedge_summary,attestations"}]
    assert get(api, strat)["data_income_usd"] == "0.01"


def test_the_service_never_receives_the_money_it_only_names_the_customers_wallet(api, strat, fac, monkeypatch):
    enable(api, strat)
    touched = []
    for name in ("transfer_from", "pay", "mint_to", "transfer_sol"):
        monkeypatch.setattr(sol, name, lambda *a, _n=name, **k: touched.append(_n))
    assert pay(api, strat)[0].status_code == 200
    assert touched == []  # no transfer was made by the service: settlement is the buyer's own signed transfer straight to pay_to
    assert dict(fac.calls)["/settle"]["paymentRequirements"]["payTo"] == CUSTOMER


def test_the_facilitator_is_always_given_our_requirements_not_the_buyers_claim(api, strat, fac):
    enable(api, strat)
    def tweak(p):
        p["accepted"]["extra"] = {"feePayer": FEE_PAYER, "memo": "buyer-chosen"}
    r, _ = pay(api, strat, tweak=tweak)
    assert r.status_code == 200
    assert dict(fac.calls)["/verify"]["paymentRequirements"]["extra"] == {"feePayer": FEE_PAYER}


@pytest.mark.parametrize("field,value", [("amount", "9999"), ("amount", "1"), ("payTo", str(Keypair().pubkey())), ("asset", str(Keypair().pubkey())),
                                         ("network", "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"), ("scheme", "upto")])
def test_a_buyer_who_signed_for_other_terms_is_refused_before_the_facilitator_is_asked(api, strat, fac, field, value):
    enable(api, strat)
    r, _ = pay(api, strat, tweak=lambda p: p["accepted"].__setitem__(field, value))
    assert r.status_code == 402 and r.json() == {"error": "Payment was not accepted.", "code": "PAYMENT_INVALID"}
    assert "PAYMENT-REQUIRED" in r.headers  # so the buyer can try again with the right terms
    assert "/verify" not in fac.paths() and feed.income(strat)[1] == 0


@pytest.mark.parametrize("header", ["not base64 !!", base64.b64encode(b"not json").decode(), base64.b64encode(b"[]").decode(),
                                    base64.b64encode(json.dumps({"x402Version": 1, "accepted": {}, "payload": {"transaction": "x"}}).encode()).decode(),
                                    base64.b64encode(json.dumps({"x402Version": 2, "accepted": {}, "payload": {}}).encode()).decode(),
                                    base64.b64encode(json.dumps({"x402Version": 2, "accepted": [], "payload": {"transaction": "x"}}).encode()).decode()])
def test_malformed_payment_headers_are_payment_invalid(api, strat, fac, header):
    enable(api, strat)
    r = api.get(feed_url(strat), headers={**KEYLESS, "PAYMENT-SIGNATURE": header})
    assert r.status_code == 402 and r.json() == {"error": "Payment was not accepted.", "code": "PAYMENT_INVALID"}
    assert "/verify" not in fac.paths()


def test_a_payment_the_facilitator_rejects_gets_no_data_and_nothing_is_recorded(api, strat, fac):
    enable(api, strat)
    fac.verify_result = {"isValid": False, "invalidReason": "invalid_exact_svm_transaction_simulation_failed", "payer": "x"}
    r, _ = pay(api, strat)
    assert r.status_code == 402 and r.json() == {"error": "Payment was not accepted.", "code": "PAYMENT_INVALID"}
    assert "invalid_exact" not in r.text  # the reason is logged, not returned
    assert "/settle" not in fac.paths() and feed.income(strat)[1] == 0


def test_a_failed_settlement_gets_no_data_and_nothing_is_recorded(api, strat, fac):
    enable(api, strat)
    fac.settle_result = {"success": False, "errorReason": "insufficient_funds", "transaction": "", "network": settings.x402_network}
    r, _ = pay(api, strat)
    assert r.status_code == 402 and r.json()["code"] == "PAYMENT_INVALID" and feed.income(strat)[1] == 0


def test_an_outage_is_facilitator_unavailable_never_payment_invalid(api, strat, fac):
    enable(api, strat)
    fac.verify_result = facilitator.unavailable("ConnectError")
    r, _ = pay(api, strat)
    assert r.status_code == 503 and r.json()["code"] == "FACILITATOR_UNAVAILABLE" and "you were not charged" in r.json()["error"]
    fac.verify_result = {"isValid": True, "payer": "BuyerWallet11111111111111111111111111111111"}
    fac.settle_result = facilitator.unavailable("ReadTimeout")  # the outage hits during settle
    x402_router._seen.clear()
    r, _ = pay(api, strat)
    assert r.status_code == 503 and r.json()["code"] == "FACILITATOR_UNAVAILABLE" and feed.income(strat)[1] == 0
    fac.settle_result = None
    fac.supported_error = facilitator.unavailable("HTTP 502")  # even the unpaid request cannot quote a fee payer
    facilitator._supported.update(at=0.0, fee_payer=None)
    assert api.get(feed_url(strat), headers=KEYLESS).json()["code"] == "FACILITATOR_UNAVAILABLE"


def test_the_real_transport_failures_map_to_unavailable(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: (_ for _ in ()).throw(httpx.ReadTimeout("slow")))
    with pytest.raises(ServiceError) as e:
        facilitator.call("/verify", {})
    assert e.value.code == "FACILITATOR_UNAVAILABLE" and e.value.status == 503
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(502, json={"isValid": True}, request=httpx.Request("POST", "https://x")))  # a 5xx is never believed, whatever the body says
    with pytest.raises(ServiceError) as e:
        facilitator.call("/settle", {})
    assert e.value.code == "FACILITATOR_UNAVAILABLE"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(200, text="<html>", request=httpx.Request("POST", "https://x")))
    with pytest.raises(ServiceError):
        facilitator.call("/verify", {})


def test_a_replayed_payment_is_served_once_and_recorded_once(api, strat, fac):
    enable(api, strat)
    first, payment = pay(api, strat)
    assert first.status_code == 200
    # the same signed transaction again: refused before the facilitator is asked a second time
    header = base64.b64encode(json.dumps(payment).encode()).decode()
    again = api.get(feed_url(strat), headers={**KEYLESS, "PAYMENT-SIGNATURE": header})
    assert again.status_code == 402 and again.json()["code"] == "PAYMENT_INVALID" and fac.paths().count("/verify") == 1
    # a different transaction that the facilitator settles to the SAME signature (a replay it re-reports): the unique ledger row stops it
    fac.settle_result = {"success": True, "payer": "BuyerWallet11111111111111111111111111111111", "transaction": "SETTLEMENT1", "network": settings.x402_network}
    other, _ = pay(api, strat)
    assert other.status_code == 402 and other.json()["code"] == "PAYMENT_INVALID"
    assert feed.income(strat)[1] == 1 and feed.income(strat)[0] == D("0.01")
    with Session(engine) as db:
        assert len(db.exec(select(DataPayment)).all()) == 1


def test_each_distinct_payment_is_recorded_once_and_income_accrues(api, strat, fac):
    enable(api, strat, price="0.5")
    for _ in range(3):
        assert pay(api, strat)[0].status_code == 200
    total, count, last = feed.income(strat)
    assert (total, count) == (D("1.5"), 3) and last is not None
    assert get(api, strat)["data_income_usd"] == "1.50" and [p["tx_signature"] for p in feed.payments(strat)] == ["SETTLEMENT3", "SETTLEMENT2", "SETTLEMENT1"]
    assert len(feed.payments(strat, limit=2)) == 2


def test_the_public_route_is_rate_limited_per_address_and_per_payer(api, strat, fac, monkeypatch):
    enable(api, strat)
    monkeypatch.setattr(settings, "x402_rate_per_min", 3)
    codes = [api.get(feed_url(strat), headers={**KEYLESS, "x-forwarded-for": "9.9.9.9"}).status_code for _ in range(5)]
    assert codes == [402, 402, 402, 429, 429]
    r = api.get(feed_url(strat), headers={**KEYLESS, "x-forwarded-for": "9.9.9.9"})
    assert r.json() == {"error": "too many requests: at most 3 per minute", "code": "RATE_LIMITED"}
    assert api.get(feed_url(strat), headers={**KEYLESS, "x-forwarded-for": "8.8.8.8"}).status_code == 402  # another address is unaffected
    x402_router._hits.clear()
    monkeypatch.setattr(settings, "x402_rate_per_min", 2)
    oks = []
    for i in range(3):
        fac.verify_result = {"isValid": True, "payer": "SameBuyer"}
        oks.append(pay(api, strat, payer=Keypair())[0].status_code)
        x402_router._hits.pop("ip:testclient", None)
    assert oks[:2] == [200, 200] and oks[2] == 429  # the same payer, three purchases in a minute


# ---- what is sold ---------------------------------------------------------------------------------------------------------------------------------

def walk(o, path=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield path + "/" + str(k), k, v
            yield from walk(v, path + "/" + str(k))
    elif isinstance(o, list):
        for i, v in enumerate(o):
            yield from walk(v, f"{path}[{i}]")


def busy_strategy(api, fakechain):
    """A strategy with every kind of internal state that must NOT leak: a live position, a target, a pending rebalance, a pending decision
    with signals, a delegate grant, and an agent's executed-looking history."""
    sid = active(api, fakechain, amount=300)["id"]
    patch(api, sid, {"target_exposure_units": "0.1"})  # a pending rebalance: target 0.06 vs holding 0.12
    decisions.store(sid, state_hash="SECRETSTATEHASH", signals_source="jev", signals_network="mainnet", question_set_version="v",
                    signals={"should_rebalance": "0.91", "needs_top_up_soon": "0.33"}, decision="propose",
                    action={"type": "rebalance", "params": {"target_size": "0.06"}}, explanation={"headline": "SECRETHEADLINE", "explanation": "e", "risk_note": ""},
                    reason="SECRETREASON")
    grant(api, sid, grant_params(rebalance_within_band_only="false", max_rebalance_oz_per_day="0.77"))
    return sid


def test_nothing_that_reveals_an_upcoming_trade_is_ever_sold(api, fakechain, fac):
    sid = busy_strategy(api, fakechain)
    view = get(api, sid)
    enable(api, sid)  # all four fields
    secrets = {str(view["position"][k]) for k in ("size_units", "margin_usd", "liquidation_price_usd", "maintenance_margin_usd", "entry_price_usd")}
    secrets |= {str(view["target_hedge_size_units"]), str(view["hedge_gap_units"]), "0.06", "0.77", "SECRETSTATEHASH", "SECRETHEADLINE", "SECRETREASON", "0.91", "0.33",
                DELEGATE.pubkey, "propose"}
    secrets.discard("0.0")
    paid, _ = pay(api, sid)
    assert paid.status_code == 200
    for label, body in (("paid", paid.json()), ("preview", api.get(f"/strategies/{sid}/data-feed/preview").json())):
        text = json.dumps(body)
        for path, key, _ in walk(body):
            assert str(key).lower() not in feed.NEVER_EXPOSE, (label, path)
        for secret in secrets:
            assert f'"{secret}"' not in text and f": {secret}" not in text and f"[{secret}" not in text, (label, secret)
        assert set(body) == {"strategy_id", "as_of", *feed.SELLABLE}
        assert set(body["hedge_summary"]) == {"hedge_ratio_pct", "hedged_share_of_exposure_pct"}


def test_the_hedge_summary_is_percentages_only(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    enable(api, sid, fields="hedge_summary")
    body = feed.build(sid, ["hedge_summary"])
    assert body["hedge_summary"] == {"hedge_ratio_pct": "60.00", "hedged_share_of_exposure_pct": "60.00"}  # a 60% hedge of 0.2 oz, fully on
    patch(api, sid, {"target_exposure_units": "0.1", "hedge_ratio_bps": "9000"})  # an edit trades nothing: it must not show up in what is sold
    assert feed.build(sid, ["hedge_summary"])["hedge_summary"] == {"hedge_ratio_pct": "60.00", "hedged_share_of_exposure_pct": "60.00"}
    patch(api, sid, {"hedge_ratio_bps": "5000"})  # a second edit before any trade: still the last executed state
    assert feed.build(sid, ["hedge_summary"])["hedge_summary"] == {"hedge_ratio_pct": "60.00", "hedged_share_of_exposure_pct": "60.00"}
    assert rebalance(api, sid)[0].status_code == 200  # now it traded: target 0.1 x 50% = 0.05 held, so the new facts are the sold facts
    assert feed.build(sid, ["hedge_summary"])["hedge_summary"] == {"hedge_ratio_pct": "50.00", "hedged_share_of_exposure_pct": "50.00"}


def seed_action(sid, name, age_s, sig="SIG", at=None):
    with Session(engine) as db:
        db.add(Action(strategy_id=sid, action=name, record={"secret": "SECRETRECORD"}, attestation_sig=sig, created_at=at - timedelta(seconds=age_s)))
        db.commit()


def test_attestations_are_withheld_for_an_hour_and_only_executed_actions_are_sold(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    with Session(engine) as db:  # start from a clean slate so the test controls every row
        for a in db.exec(select(Action).where(Action.strategy_id == sid)).all():
            db.delete(a)
        db.commit()
    at = now()
    delay = settings.x402_attestation_delay_s
    assert delay == 3600
    seed_action(sid, "rebalance", delay - 1, "TOO_NEW", at)  # one second short of the delay: withheld
    seed_action(sid, "rebalance", delay, "EXACTLY_ON_TIME", at)  # exactly an hour old: released
    seed_action(sid, "deploy", delay + 600, "DEPLOY", at)
    seed_action(sid, "close", 5, "VERY_NEW_CLOSE", at)
    seed_action(sid, "rebalance", delay + 60, None, at)  # a no-op rebalance has no attestation: nothing to sell
    for hidden in ("grant_delegate", "revoke_delegate", "edit_hedge_settings", "change_owner", "dismiss_decision", "ledger_adjustment",
                   "configure_data_feed", "set_owner", "deploy_failed", "refund", "withdrawal_failed"):
        seed_action(sid, hidden, delay * 5, "HIDDEN_" + hidden, at)  # old enough, but not an executed trade: never sold
    body = feed.build(sid, ["attestations"], at)["attestations"]
    assert body["delay_s"] == 3600
    assert sorted(i["attestation_sig"] for i in body["items"]) == ["DEPLOY", "EXACTLY_ON_TIME"]
    assert "SECRETRECORD" not in json.dumps(body) and all(set(i) == {"action", "at", "attestation_sig", "explorer_url"} for i in body["items"])
    later = feed.build(sid, ["attestations"], at + timedelta(seconds=2))["attestations"]  # two seconds on, the 3599s-old one is now an hour old
    assert "TOO_NEW" in [i["attestation_sig"] for i in later["items"]] and "VERY_NEW_CLOSE" not in [i["attestation_sig"] for i in later["items"]]
    assert "VERY_NEW_CLOSE" in [i["attestation_sig"] for i in feed.build(sid, ["attestations"], at + timedelta(hours=2))["attestations"]["items"]]


def test_the_delay_is_not_settable_from_outside_and_is_documented():
    import pathlib
    assert settings.x402_attestation_delay_s == 3600
    root = pathlib.Path(__file__).resolve().parent.parent
    assert "X402_ATTESTATION_DELAY_S=3600" in (root / ".env.example").read_text()


def test_nav_is_unavailable_with_a_reason_until_published_never_a_placeholder(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    body = feed.build(sid, ["nav_per_share", "nav_history"])
    assert body["nav_per_share"] == {"status": "unavailable", "reason": "no NAV has been published for this strategy yet"}
    assert body["nav_history"] == {"status": "unavailable", "reason": "no NAV has been published for this strategy yet"}
    assert not any(ch.isdigit() for ch in json.dumps({k: body[k] for k in ("nav_per_share", "nav_history")}).replace("no NAV has been", ""))


def nav_req(api, sid, params, signer=OWNER, **kw):
    return api.post(f"/strategies/{sid}/nav", json={**params, "authorization": signer.authorization("publish_nav", sid, params, **kw)})


def nav_params(offset_s=0, nav="1.0234", unh="1.0198"):
    return {"nav_per_share": nav, "unhedged_nav_per_share": unh, "as_of": (now() + timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def test_publishing_nav_stores_it_attests_it_and_serves_it(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    p = nav_params(-120)
    r = nav_req(api, sid, p)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"nav_per_share", "unhedged_nav_per_share", "as_of", "attestation_sig"} and r.json()["attestation_sig"]
    assert (r.json()["nav_per_share"], r.json()["unhedged_nav_per_share"], r.json()["as_of"]) == (p["nav_per_share"], p["unhedged_nav_per_share"], p["as_of"])
    memo = json.loads(fakechain.memos[-1])
    with Session(engine) as db:
        a = db.exec(select(Action).where(Action.action == "publish_nav")).one()
    assert memo["a"] == "publish_nav" and memo["h"] == att.record_hash(a.record) and a.record["signed_by"] == OWNER.pubkey
    nav = feed.build(sid, ["nav_per_share", "nav_history"])
    assert nav["nav_per_share"] == {"status": "available", "hedged": "1.0234", "unhedged": "1.0198", "as_of": p["as_of"]}
    nav_req(api, sid, nav_params(-60, "1.0300", "1.0250"))
    h = feed.build(sid, ["nav_history"])["nav_history"]["checkpoints"]
    assert [c["hedged"] for c in h] == ["1.0300", "1.0234"]  # newest first
    assert feed.build(sid, ["nav_per_share"])["nav_per_share"]["hedged"] == "1.0300"


def test_nav_as_of_rules_stale_future_and_non_positive_are_rejected_without_burning_the_signature(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    assert nav_req(api, sid, nav_params(-60)).status_code == 200
    used = nonces()
    stale = nav_req(api, sid, nav_params(-600))
    assert stale.status_code == 400 and "older than the latest published checkpoint" in stale.json()["error"]
    future = nav_req(api, sid, nav_params(+31))
    assert future.status_code == 400 and "more than 30 seconds in the future" in future.json()["error"]
    assert nav_req(api, sid, nav_params(0, nav="0")).json()["error"] == "nav_per_share must be positive"
    assert nav_req(api, sid, nav_params(0, unh="0.00")).json()["error"] == "unhedged_nav_per_share must be positive"
    assert nav_req(api, sid, {**nav_params(0), "as_of": "2026-10-15"}).json()["error"].startswith("as_of must be an ISO 8601 UTC time")
    assert nav_req(api, sid, {**nav_params(0), "as_of": "2026-13-45T99:99:99Z"}).json()["error"] == "as_of is not a real date"
    assert nonces() == used
    with Session(engine) as db:
        assert len(db.exec(select(NavCheckpoint)).all()) == 1
    assert nav_req(api, sid, nav_params(+25)).status_code == 200  # 25 s ahead is within the 30 s skew
    equal = nav_params(0)
    same = nav_req(api, sid, {**equal, "as_of": feed.iso(feed.latest_checkpoint(sid).as_of)})
    assert same.status_code == 200  # not older than the latest: allowed


def test_nav_publishing_needs_positive_decimal_strings_and_every_field(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    p = nav_params(-30)
    assert nav_req(api, sid, {"nav_per_share": "1.0"}).status_code == 400
    r = api.post(f"/strategies/{sid}/nav", json={**p, "nav_per_share": 1.02, "authorization": OWNER.authorization("publish_nav", sid, p)})
    assert r.status_code == 403 and r.json()["code"] == "AUTHORIZATION_INVALID"
    bad = {**p, "nav_per_share": "-1.0"}
    assert nav_req(api, sid, bad).json()["code"] == "AUTHORIZATION_INVALID"  # a sign is not part of the canonical decimal format


def test_only_the_owner_publishes_nav_not_a_stranger_not_a_delegate(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    p = nav_params(-30)
    assert api.post(f"/strategies/{sid}/nav", json=p).json()["code"] == "AUTHORIZATION_REQUIRED"
    assert nav_req(api, sid, p, signer=Signer()).json()["code"] == "AUTHORIZATION_INVALID"
    grant(api, sid, grant_params(rebalance_within_band_only="false"))
    r = nav_req(api, sid, p, signer=DELEGATE)
    assert r.status_code == 403 and r.json()["code"] == "DELEGATE_NOT_ALLOWED"
    with Session(engine) as db:
        assert db.exec(select(NavCheckpoint)).all() == []


def test_preview_is_exactly_what_a_buyer_gets_and_needs_the_service_key(api, fakechain, fac):
    sid = active(api, fakechain)["id"]
    nav_req(api, sid, nav_params(-90))
    enable(api, sid, fields="nav_per_share,hedge_summary")
    paid, _ = pay(api, sid)
    prev = api.get(f"/strategies/{sid}/data-feed/preview").json()
    strip = lambda d: {k: v for k, v in d.items() if k != "as_of"}  # noqa: E731
    assert strip(prev) == strip(paid.json()) and set(prev) == {"strategy_id", "as_of", "nav_per_share", "hedge_summary"}
    assert api.get(f"/strategies/{sid}/data-feed/preview", headers={"X-Sereel-Key": "wrong"}).status_code == 401
    assert api.get(f"/strategies/{sid}/data-feed", headers={"X-Sereel-Key": "wrong"}).status_code == 401
    assert api.get(f"/strategies/{sid}/data-feed/payments", headers={"X-Sereel-Key": "wrong"}).status_code == 401
    assert api.get("/strategies/nope/data-feed").status_code == 404


# ---- the demo buyer --------------------------------------------------------------------------------------------------------------------------------------

def test_the_demo_buyers_transaction_follows_the_scheme_and_carries_only_the_buyers_signature(strat_unused=None):
    payer = Keypair()
    accepted = {"scheme": "exact", "network": settings.x402_network, "amount": "10000", "asset": settings.x402_usdc_mint, "payTo": CUSTOMER,
                "maxTimeoutSeconds": 60, "extra": {"feePayer": FEE_PAYER}}
    tx = client.build_payment_transaction(payer, accepted, BLOCKHASH)
    b64 = base64.b64encode(bytes(tx)).decode()
    assert client.check_structure(b64, accepted) == []
    decoded = VersionedTransaction.from_bytes(bytes(tx))
    assert str(decoded.message.account_keys[0]) == FEE_PAYER and decoded.signatures[0] == __import__("solders.signature", fromlist=["Signature"]).Signature.default()
    assert decoded.signatures[1] != decoded.signatures[0]  # slot 0 is the facilitator's (empty), slot 1 is the buyer's
    # two payments are never the same bytes (a random memo nonce), so a replay cannot be mistaken for a fresh purchase
    assert bytes(client.build_payment_transaction(payer, accepted, BLOCKHASH)) != bytes(tx)
    wrong = {**accepted, "payTo": str(Keypair().pubkey())}
    assert any("destination" in p for p in client.check_structure(b64, wrong))
    assert any("amount" in p for p in client.check_structure(b64, {**accepted, "amount": "1"}))


def test_the_demo_buyer_buys_end_to_end_against_the_service(api, strat, fac, monkeypatch):
    enable(api, strat, fields="hedge_summary")
    monkeypatch.setattr(sol, "rpc", lambda method, params=None, **k: {"value": {"blockhash": BLOCKHASH}})
    http = api  # the test client is an httpx client: no network needed
    res = client.buy(feed_url(strat), Keypair(), client=http)
    assert res["data"]["hedge_summary"]["hedge_ratio_pct"] == "60.00" and res["settlement"]["transaction"] == "SETTLEMENT1" and res["price"] == "10000"
    assert feed.income(strat)[1] == 1
    with pytest.raises(client.BuyError, match="no data feed"):
        client.buy(feed_url("nope"), Keypair(), client=http)


def test_the_cli_command_exists_and_validates_its_arguments():
    r = runner.invoke(cli_app, ["x402", "buy", "--help"])
    assert r.exit_code == 0 and "--keypair" in r.output and "--repeat" in r.output and "--interval" in r.output
    assert runner.invoke(cli_app, ["x402", "buy", "abc"]).exit_code != 0  # --keypair is required


def test_the_new_codes_are_registered_and_the_402_message_is_exact():
    assert {"DATA_FEED_DISABLED", "PAYMENT_INVALID", "FACILITATOR_UNAVAILABLE", "RATE_LIMITED"} <= set(CODES)
    assert requirements.NOT_ACCEPTED == "Payment was not accepted." and CODES["PAYMENT_INVALID"][0] == 402 and CODES["FACILITATOR_UNAVAILABLE"][0] == 503


def test_every_http_call_in_the_x402_package_has_a_timeout():
    import ast
    import pathlib
    seen = 0
    for path in (pathlib.Path(__file__).resolve().parent.parent / "app" / "x402").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx" \
                    and node.func.attr in ("get", "post", "Client"):
                seen += 1
                assert any(k.arg == "timeout" for k in node.keywords), f"{path.name}:{node.lineno}"
    assert seen >= 3
