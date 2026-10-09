"""A third-party buyer, for demos and tests: ask, receive the 402, pay, retry, read the data.

The payment is the x402 `exact` Solana scheme: a partially signed versioned transaction that transfers USDC from the buyer to the
customer's token account. The facilitator is the fee payer and signs second, so the buyer needs Circle devnet USDC but no SOL. Required
shape (per the scheme): compute-unit limit, compute-unit price, TransferChecked, then a memo carrying a random nonce."""
import base64
import json
import os
import time

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from spl.memo.constants import MEMO_PROGRAM_ID
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address, transfer_checked
from spl.token.models import TransferCheckedParams

from .. import solana_client as sol

DECIMALS = 6
COMPUTE_LIMIT, COMPUTE_PRICE = 40_000, 1


class BuyError(Exception):
    pass


def build_payment_transaction(payer: Keypair, accepted: dict, blockhash: str) -> VersionedTransaction:
    """Partially signed: slot 0 (the facilitator, the fee payer) is left empty, slot 1 is the buyer's signature."""
    fee_payer = Pubkey.from_string(accepted["extra"]["feePayer"])
    mint, pay_to = Pubkey.from_string(accepted["asset"]), Pubkey.from_string(accepted["payTo"])
    ixs = [
        set_compute_unit_limit(COMPUTE_LIMIT),
        set_compute_unit_price(COMPUTE_PRICE),
        transfer_checked(TransferCheckedParams(program_id=TOKEN_PROGRAM_ID, source=get_associated_token_address(payer.pubkey(), mint), mint=mint,
                                               dest=get_associated_token_address(pay_to, mint), owner=payer.pubkey(), amount=int(accepted["amount"]),
                                               decimals=DECIMALS, signers=[])),
        Instruction(MEMO_PROGRAM_ID, os.urandom(16).hex().encode(), []),  # a random nonce: two payments are never the same bytes
    ]
    msg = MessageV0.try_compile(fee_payer, ixs, [], Hash.from_string(blockhash))
    return VersionedTransaction.populate(msg, [Signature.default(), payer.sign_message(to_bytes_versioned(msg))])


def payment_header(payer: Keypair, required: dict, blockhash: str) -> str:
    accepted = required["accepts"][0]
    tx = build_payment_transaction(payer, accepted, blockhash)
    payload = {"x402Version": 2, "resource": required.get("resource"), "accepted": accepted,
               "payload": {"transaction": base64.b64encode(bytes(tx)).decode()}}
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def check_structure(tx_b64: str, accepted: dict) -> list[str]:
    """The scheme's structural rules, as a list of problems (empty = conforms). Used by the tests and as a pre-flight check."""
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    msg = tx.message
    keys = [str(k) for k in msg.account_keys]
    problems = []
    ixs = msg.instructions
    if not 3 <= len(ixs) <= 6:
        problems.append(f"{len(ixs)} instructions (3-6 allowed)")
        return problems
    prog = lambda i: keys[ixs[i].program_id_index]  # noqa: E731
    cb = "ComputeBudget111111111111111111111111111111"
    if prog(0) != cb or ixs[0].data[:1] != bytes([2]):
        problems.append("instruction 0 must be SetComputeUnitLimit")
    if prog(1) != cb or ixs[1].data[:1] != bytes([3]):
        problems.append("instruction 1 must be SetComputeUnitPrice")
    if prog(2) != str(TOKEN_PROGRAM_ID) or ixs[2].data[:1] != bytes([12]):
        problems.append("instruction 2 must be a TransferChecked")
    else:
        accts = [keys[i] for i in ixs[2].accounts]  # source, mint, destination, authority
        dest = str(get_associated_token_address(Pubkey.from_string(accepted["payTo"]), Pubkey.from_string(accepted["asset"])))
        if accts[2] != dest:
            problems.append("destination is not the pay_to's associated token account")
        if accts[1] != accepted["asset"]:
            problems.append("wrong mint")
        if int.from_bytes(ixs[2].data[1:9], "little") != int(accepted["amount"]):
            problems.append("amount does not match")
        if accts[3] == keys[0] or accts[0] == keys[0]:
            problems.append("the fee payer is the transfer's source or authority")
    for i in range(3, len(ixs)):
        if prog(i) not in (str(MEMO_PROGRAM_ID), "L2TExMFKdjpN9kozasaurPirfHy9P8sbXoAN1qA3S95"):
            problems.append(f"instruction {i} is not a memo or lighthouse instruction")
    if keys[0] != accepted["extra"]["feePayer"]:
        problems.append("the fee payer is not the facilitator's")
    for ix in ixs:
        if keys[0] in [keys[a] for a in ix.accounts]:
            problems.append("the fee payer appears in an instruction's accounts")
            break
    return problems


def buy(url: str, payer: Keypair, client: httpx.Client | None = None) -> dict:
    """One purchase. Returns {data, settlement (decoded PAYMENT-RESPONSE)}. Raises BuyError with the server's own words."""
    http = client or httpx.Client(timeout=30)
    try:
        first = http.get(url)
        if first.status_code == 404:
            raise BuyError(f"{first.json().get('error', 'no data feed here')} (404)")
        if first.status_code != 402:
            raise BuyError(f"expected a 402 first, got {first.status_code}: {first.text[:200]}")
        required = json.loads(base64.b64decode(first.headers["PAYMENT-REQUIRED"]))
        blockhash = sol.rpc("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
        paid = http.get(url, headers={"PAYMENT-SIGNATURE": payment_header(payer, required, blockhash)})
        if paid.status_code != 200:
            raise BuyError(f"payment refused: {paid.status_code} {paid.text[:200]}")
        settlement = json.loads(base64.b64decode(paid.headers["PAYMENT-RESPONSE"]))
        return {"data": paid.json(), "settlement": settlement, "price": required["accepts"][0]["amount"]}
    finally:
        if client is None:
            http.close()
