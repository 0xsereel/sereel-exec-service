"""Sync Solana helper: JSON-RPC over httpx, transactions via solders/spl builders."""
import base64
import json
import time
from decimal import Decimal
from pathlib import Path

import httpx
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, create_account
from solders.transaction import Transaction
from spl.memo.instructions import create_memo
from spl.memo.models import MemoParams
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    create_idempotent_associated_token_account,
    get_associated_token_address,
    initialize_mint,
    mint_to_checked,
    transfer_checked,
)
from spl.token.models import InitializeMintParams, MintToCheckedParams, TransferCheckedParams

from .config import settings

DECIMALS = 6
MINT_ACCOUNT_SIZE = 82
MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
_http = httpx.Client(timeout=30)


class SolanaError(Exception):
    pass


def rpc(method: str, params: list | None = None, retries: int = 4):
    """JSON-RPC call; retries 429/5xx and transport errors with exponential backoff."""
    for attempt in range(retries + 1):
        try:
            r = _http.post(settings.solana_rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []})
            if r.status_code == 429 or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            break
        except httpx.TransportError as e:
            err = e
        except httpx.HTTPStatusError as e:
            err = e
        if attempt == retries:
            raise SolanaError(f"{method}: RPC unavailable after {retries + 1} attempts ({err})")
        time.sleep(min(0.5 * 2**attempt, 8))
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise SolanaError(f"{method}: {body['error']}")
    return body["result"]


# ---- keys -------------------------------------------------------------------

def load_keypair(path: str, create: bool = False) -> Keypair:
    p = settings.resolve(path)
    if not p.exists():
        if not create:
            raise FileNotFoundError(f"{p} missing; run `sereel init`")
        p.parent.mkdir(parents=True, exist_ok=True)
        kp = Keypair()
        p.write_text(json.dumps(list(bytes(kp))))
        p.chmod(0o600)
        return kp
    return Keypair.from_bytes(bytes(json.loads(Path(p).read_text())))


def funding_kp() -> Keypair:
    return load_keypair(settings.funding_keypair)


def attest_kp() -> Keypair:
    return load_keypair(settings.attest_keypair)


def mint_authority_kp() -> Keypair:
    return load_keypair(settings.mint_authority_keypair)


def payment_source_kp() -> Keypair:
    return load_keypair(settings.payment_source_keypair)


def mint_pubkey() -> Pubkey:
    if not settings.stablecoin_mint:
        raise SolanaError("STABLECOIN_MINT not set; run `sereel init`")
    return Pubkey.from_string(settings.stablecoin_mint)


def is_devnet() -> bool:
    return any(h in settings.solana_rpc_url for h in ("devnet", "localhost", "127.0.0.1"))


def own_addresses() -> set[str]:
    """Addresses the service controls; the watcher never refunds or credits transfers from these."""
    out = set()
    for path in (settings.funding_keypair, settings.attest_keypair, settings.payment_source_keypair, settings.mint_authority_keypair):
        try:
            out.add(str(load_keypair(path).pubkey()))
        except FileNotFoundError:
            pass
    return out


def is_valid_address(addr: str) -> bool:
    try:
        Pubkey.from_string(addr)
        return True
    except Exception:
        return False


def is_multisig_address(addr: str) -> bool:
    """A Squads vault is a PDA, i.e. off the ed25519 curve; an ordinary wallet is on it."""
    return not Pubkey.from_string(addr).is_on_curve()


def explorer_url(signature: str) -> str:
    cluster = "?cluster=devnet" if is_devnet() else ""
    return f"https://explorer.solana.com/tx/{signature}{cluster}"


# ---- amounts ----------------------------------------------------------------

def to_base(amount: Decimal) -> int:
    return int((Decimal(amount) * 10**DECIMALS).to_integral_value())


def from_base(raw: int | str) -> Decimal:
    return Decimal(int(raw)) / 10**DECIMALS


# ---- sending ----------------------------------------------------------------

def send(instructions: list, signers: list[Keypair], commitment: str = "confirmed", timeout: float = 60) -> str:
    bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"])
    tx = Transaction(signers, Message.new_with_blockhash(instructions, signers[0].pubkey(), bh), bh)
    sig = rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64", "preflightCommitment": "confirmed"}])
    ok = ("confirmed", "finalized") if commitment == "confirmed" else ("finalized",)
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = rpc("getSignatureStatuses", [[sig]])["value"][0]
        if st:
            if st.get("err"):
                raise SolanaError(f"tx {sig} failed: {st['err']}")
            if st.get("confirmationStatus") in ok:
                return sig
        time.sleep(1)
    raise SolanaError(f"tx {sig} not {commitment} within {timeout}s")


def airdrop(pubkey: Pubkey, sol: float = 2) -> str | None:
    try:
        return rpc("requestAirdrop", [str(pubkey), int(sol * 1e9)])
    except Exception:
        return None


def sol_balance(pubkey: Pubkey) -> Decimal:
    return Decimal(rpc("getBalance", [str(pubkey)])["value"]) / Decimal(10**9)


# ---- token ------------------------------------------------------------------

def create_mint(payer: Keypair, mint_authority: Keypair) -> Pubkey:
    mint = Keypair()
    rent = rpc("getMinimumBalanceForRentExemption", [MINT_ACCOUNT_SIZE])
    send([
        create_account(CreateAccountParams(from_pubkey=payer.pubkey(), to_pubkey=mint.pubkey(), lamports=rent,
                                           space=MINT_ACCOUNT_SIZE, owner=TOKEN_PROGRAM_ID)),
        initialize_mint(InitializeMintParams(decimals=DECIMALS, program_id=TOKEN_PROGRAM_ID, mint=mint.pubkey(),
                                             mint_authority=mint_authority.pubkey(), freeze_authority=None)),
    ], [payer, mint])
    return mint.pubkey()


def ata(owner: Pubkey | str) -> Pubkey:
    return get_associated_token_address(Pubkey.from_string(str(owner)), mint_pubkey())


def token_balance(owner: Pubkey | str) -> Decimal:
    res = rpc("getTokenAccountsByOwner", [str(owner), {"mint": str(mint_pubkey())}, {"encoding": "jsonParsed"}])
    return sum((Decimal(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"]) for a in res["value"]), Decimal(0))


def _memo_ix(memo: str, signer: Pubkey):
    return create_memo(MemoParams(program_id=MEMO_PROGRAM, signer=signer, message=memo.encode()))


def mint_to(to_owner: str, amount: Decimal, memo: str | None = None) -> str:
    """Devnet only: mint stablecoin to a wallet's ATA."""
    if not is_devnet():
        raise SolanaError("mint is only available on devnet")
    auth = mint_authority_kp()
    ixs = [
        create_idempotent_associated_token_account(auth.pubkey(), Pubkey.from_string(to_owner), mint_pubkey()),
        mint_to_checked(MintToCheckedParams(program_id=TOKEN_PROGRAM_ID, mint=mint_pubkey(), dest=ata(to_owner),
                                            mint_authority=auth.pubkey(), amount=to_base(amount), decimals=DECIMALS)),
    ]
    if memo:
        ixs.append(_memo_ix(memo, auth.pubkey()))
    return send(ixs, [auth])


def transfer_from(source: Keypair, to_owner: str, amount: Decimal, memo: str | None = None) -> str:
    """Transfer stablecoin from source's ATA to a wallet's ATA (created if needed)."""
    ixs = [
        create_idempotent_associated_token_account(source.pubkey(), Pubkey.from_string(to_owner), mint_pubkey()),
        transfer_checked(TransferCheckedParams(program_id=TOKEN_PROGRAM_ID, source=ata(source.pubkey()), mint=mint_pubkey(),
                                               dest=ata(to_owner), owner=source.pubkey(), amount=to_base(amount),
                                               decimals=DECIMALS, signers=[])),
    ]
    if memo:
        ixs.append(_memo_ix(memo, source.pubkey()))
    return send(ixs, [source])


def pay(to_owner: str, amount: Decimal, memo: str | None = None, source: Keypair | None = None) -> str:
    """Pay from `source` (default: payment source). If it is short, mint on devnet, else fail."""
    source = source or payment_source_kp()
    if token_balance(source.pubkey()) < amount:
        if not is_devnet():
            raise SolanaError("insufficient stablecoin in source wallet")
        return mint_to(to_owner, amount, memo)
    return transfer_from(source, to_owner, amount, memo)


def post_memo(memo: str) -> str:
    kp = attest_kp()
    return send([_memo_ix(memo, kp.pubkey())], [kp])


# ---- inbound transfers (watcher) -------------------------------------------

def parse_inbound(tx: dict, signature: str, dest_owner: str) -> dict | None:
    """From a jsonParsed tx: {signature, sender, amount, memo} for stablecoin received by dest_owner, else None."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return None
    mint = str(mint_pubkey())

    def bal(entries):
        out: dict[str, Decimal] = {}
        for b in entries or []:
            if b["mint"] == mint:
                out[b["owner"]] = out.get(b["owner"], Decimal(0)) + Decimal(b["uiTokenAmount"]["uiAmountString"] or 0)
        return out

    pre, post = bal(meta.get("preTokenBalances")), bal(meta.get("postTokenBalances"))
    if post.get(dest_owner, Decimal(0)) - pre.get(dest_owner, Decimal(0)) <= 0:
        return None
    amount = post[dest_owner] - pre.get(dest_owner, Decimal(0))
    sender = next((o for o, after in post.items() if o != dest_owner and after < pre.get(o, Decimal(0))), None)
    ixs = tx["transaction"]["message"]["instructions"] + [i for g in meta.get("innerInstructions") or [] for i in g["instructions"]]
    memo = next((i["parsed"] for i in ixs if i.get("program") == "spl-memo" and isinstance(i.get("parsed"), str)), None)
    return {"signature": signature, "sender": sender, "amount": amount, "memo": memo}


def get_parsed_tx(signature: str, commitment: str = "finalized") -> dict | None:
    return rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": commitment, "maxSupportedTransactionVersion": 0}])


def finalized_signatures_since(address: str, until: str | None, limit: int = 200, max_pages: int = 20) -> list[str]:
    """Finalized, successful signatures for `address` newer than `until`, oldest first.
    Pages backwards (the RPC returns newest first) until it reaches the cursor. With no cursor, or a cursor
    more than max_pages old, only the newest max_pages * limit signatures are returned."""
    out: list[str] = []
    before = None
    for _ in range(max_pages):
        opts = {"commitment": "finalized", "limit": min(limit, 1000)}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [address, opts])
        out += [e["signature"] for e in page if not e.get("err")]
        if len(page) < opts["limit"]:
            break
        before = page[-1]["signature"]
    return list(reversed(out))
