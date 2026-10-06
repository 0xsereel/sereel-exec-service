import time
from decimal import Decimal

import httpx
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app import pyth
from app import solana_client as sol
from app.config import settings
from app.errors import ServiceError

MINT = str(Keypair().pubkey())
FUNDING, SENDER = str(Keypair().pubkey()), str(Keypair().pubkey())


@pytest.fixture(autouse=True)
def mint(monkeypatch):
    monkeypatch.setattr(settings, "stablecoin_mint", MINT)


def tx(pre, post, memo=None, err=None):
    bal = lambda rows: [{"mint": MINT, "owner": o, "uiTokenAmount": {"uiAmountString": a}} for o, a in rows]
    ixs = [{"program": "spl-token", "parsed": {}}]
    if memo:
        ixs.append({"program": "spl-memo", "parsed": memo})
    return {"meta": {"err": err, "preTokenBalances": bal(pre), "postTokenBalances": bal(post), "innerInstructions": []},
            "transaction": {"message": {"instructions": ixs}}}


def test_amounts_roundtrip():
    assert sol.to_base(Decimal("1.5")) == 1_500_000 and sol.from_base(1_500_000) == Decimal("1.5")


def test_parse_inbound_extracts_sender_amount_memo():
    t = tx([(FUNDING, "10"), (SENDER, "500")], [(FUNDING, "110"), (SENDER, "400")], memo="intent-123")
    assert sol.parse_inbound(t, "sig", FUNDING) == {"signature": "sig", "sender": SENDER, "amount": Decimal(100), "memo": "intent-123"}


def test_parse_inbound_ignores_failed_outgoing_and_other_mints():
    assert sol.parse_inbound(tx([(FUNDING, "10")], [(FUNDING, "110")], err={"x": 1}), "s", FUNDING) is None
    assert sol.parse_inbound(tx([(FUNDING, "110")], [(FUNDING, "10")]), "s", FUNDING) is None  # outgoing
    other = tx([], [(FUNDING, "5")]); other["meta"]["postTokenBalances"][0]["mint"] = "other"
    assert sol.parse_inbound(other, "s", FUNDING) is None


def test_parse_inbound_without_memo():
    assert sol.parse_inbound(tx([], [(FUNDING, "5")]), "s", FUNDING)["memo"] is None


def test_multisig_detection_on_vs_off_curve():
    assert sol.is_multisig_address(SENDER) is False  # keypair pubkeys are on-curve
    pda, _ = Pubkey.find_program_address([b"vault"], Pubkey.from_string(MINT))
    assert sol.is_multisig_address(str(pda)) is True


def test_address_validation_and_own_addresses(tmp_path, monkeypatch):
    assert sol.is_valid_address(SENDER) and not sol.is_valid_address("nope")
    p = tmp_path / "k.json"
    kp = sol.load_keypair(str(p), create=True)
    monkeypatch.setattr(settings, "funding_keypair", str(p))
    assert str(kp.pubkey()) in sol.own_addresses() and oct(p.stat().st_mode & 0o777) == "0o600"


def test_pyth_mock_price(monkeypatch):
    assert pyth.get_price("0xab").price == Decimal(2650)


def hermes(monkeypatch, status=200, publish_time=None, price="265000", expo=-2):
    monkeypatch.setattr(settings, "pyth_mock_price", None)
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen["headers"] = headers
        body = {"parsed": [{"price": {"price": price, "expo": expo, "publish_time": publish_time or int(time.time())}}]}
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_pyth_parses_exponent_and_sends_key(monkeypatch):
    monkeypatch.setattr(settings, "pyth_api_key", "k")
    seen = hermes(monkeypatch)
    assert pyth.get_price("ab").price == Decimal("2650.00") and seen["headers"] == {"Authorization": "Bearer k"}


def test_pyth_stale_and_rejected(monkeypatch):
    hermes(monkeypatch, publish_time=int(time.time()) - 120)
    with pytest.raises(ServiceError) as e:
        pyth.get_price("ab", max_staleness_s=30)
    assert e.value.code == "STALE_PRICE"
    assert pyth.get_price("ab", max_staleness_s=None).price  # check can be disabled


@pytest.mark.parametrize("status", [401, 403])
def test_pyth_auth_failure_has_distinct_code(monkeypatch, status):
    hermes(monkeypatch, status=status)
    with pytest.raises(ServiceError) as e:
        pyth.get_price("ab")
    assert e.value.code == "PRICE_SOURCE_AUTH" and "PYTH_API_KEY" in e.value.message and e.value.status == 502


def test_pyth_search_auth_failure_also_distinct(monkeypatch):
    hermes(monkeypatch, status=401)
    with pytest.raises(ServiceError) as e:
        pyth.search_feeds("XAU")
    assert e.value.code == "PRICE_SOURCE_AUTH"


def test_pyth_default_base_url():
    from app.config import Settings
    assert Settings(_env_file=None).pyth_hermes_url == "https://pyth.dourolabs.app/hermes"


def test_rpc_retries_throttling_then_succeeds(monkeypatch):
    calls = []

    def fake_post(url, json=None):
        calls.append(1)
        code = 429 if len(calls) < 3 else 200
        return httpx.Response(code, json={"result": 7}, request=httpx.Request("POST", url))

    monkeypatch.setattr(sol._http, "post", fake_post)
    monkeypatch.setattr(sol.time, "sleep", lambda s: None)
    assert sol.rpc("getSlot") == 7 and len(calls) == 3


def test_rpc_gives_up_with_solana_error(monkeypatch):
    monkeypatch.setattr(sol._http, "post", lambda url, json=None: httpx.Response(429, request=httpx.Request("POST", url)))
    monkeypatch.setattr(sol.time, "sleep", lambda s: None)
    with pytest.raises(sol.SolanaError):
        sol.rpc("getSlot", retries=2)


def test_signature_paging_oldest_first_skips_failed_and_chains_before(monkeypatch):
    pages = [[{"signature": "s5", "err": None}, {"signature": "s4", "err": None}],
             [{"signature": "s3", "err": {"x": 1}}, {"signature": "s2", "err": None}],
             [{"signature": "s1", "err": None}]]
    seen = []

    def fake_rpc(method, params):
        seen.append(params[1])
        return pages[len(seen) - 1]

    monkeypatch.setattr(sol, "rpc", fake_rpc)
    assert sol.finalized_signatures_since("A", "cursor", limit=2) == ["s1", "s2", "s4", "s5"]
    assert [o.get("before") for o in seen] == [None, "s4", "s2"]
    assert all(o["until"] == "cursor" and o["commitment"] == "finalized" for o in seen)


def test_signature_paging_is_capped(monkeypatch):
    monkeypatch.setattr(sol, "rpc", lambda m, p: [{"signature": f"x{len(p)}", "err": None}] * 2)
    assert len(sol.finalized_signatures_since("A", None, limit=2, max_pages=3)) == 6


def test_a_send_whose_first_attempt_landed_is_confirmed_not_failed(monkeypatch):
    """The retry of a lost-response send gets AlreadyProcessed: the transaction is on-chain, so it must succeed."""
    kp = Keypair()
    calls = []

    def fake_rpc(method, params=None, retries=4):
        calls.append(method)
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(__import__("solders.hash", fromlist=["Hash"]).Hash.default())}}
        if method == "sendTransaction":
            raise sol.SolanaError("sendTransaction: {'code': -32002, 'message': 'Transaction simulation failed: This transaction has "
                                  "already been processed', 'data': {'err': 'AlreadyProcessed'}}")
        if method == "getSignatureStatuses":
            return {"value": [{"err": None, "confirmationStatus": "confirmed"}]}
        raise AssertionError(method)

    monkeypatch.setattr(sol, "rpc", fake_rpc)
    from solders.system_program import TransferParams, transfer

    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    sig = sol.send([ix], [kp])
    assert isinstance(sig, str) and len(sig) > 40 and "getSignatureStatuses" in calls  # confirmed by its own signature


def test_other_send_errors_still_fail(monkeypatch):
    kp = Keypair()

    def fake_rpc(method, params=None, retries=4):
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": str(__import__("solders.hash", fromlist=["Hash"]).Hash.default())}}
        raise sol.SolanaError("sendTransaction: {'code': -32002, 'message': 'Transaction simulation failed: insufficient funds'}")

    monkeypatch.setattr(sol, "rpc", fake_rpc)
    from solders.system_program import TransferParams, transfer

    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Keypair().pubkey(), lamports=1))
    with pytest.raises(sol.SolanaError, match="insufficient funds"):
        sol.send([ix], [kp])
