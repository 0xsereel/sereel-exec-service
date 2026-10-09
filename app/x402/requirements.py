"""What a buyer must pay, built from OUR stored configuration (never from the request), and the customer's token account."""
import base64
import json
import logging
from decimal import Decimal, InvalidOperation

from solders.pubkey import Pubkey
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import create_idempotent_associated_token_account, get_associated_token_address

from .. import solana_client as sol
from ..config import settings
from ..errors import ServiceError
from . import facilitator

log = logging.getLogger("sereel.x402")
USDC_DECIMALS = 6
NOT_ACCEPTED = "Payment was not accepted."


def invalid(headers: dict | None = None) -> ServiceError:
    """PAYMENT_INVALID: the single, fixed message for every reason a payment fails (the detail is logged, not returned)."""
    return ServiceError("PAYMENT_INVALID", NOT_ACCEPTED, 402, headers)


def to_base_units(price_usd: str) -> int:
    d = Decimal(price_usd)
    raw = d * (10 ** USDC_DECIMALS)
    if raw != raw.to_integral_value():
        raise ValueError(f"price_usd supports at most {USDC_DECIMALS} decimals")
    return int(raw)


def check_price(price: str) -> str:
    try:
        d = Decimal(price)
    except InvalidOperation:
        raise ServiceError("BAD_REQUEST", "price_usd must be a decimal string such as \"0.01\"", 400)
    if d <= 0 or d > 1000:
        raise ServiceError("BAD_REQUEST", "price_usd must be above 0 and at most 1000", 400)
    try:
        to_base_units(price)
    except ValueError as e:
        raise ServiceError("BAD_REQUEST", str(e), 400)
    return price


def requirements(pay_to: str, price_usd: str) -> dict:
    return {"scheme": "exact", "network": settings.x402_network, "amount": str(to_base_units(price_usd)), "asset": settings.x402_usdc_mint,
            "payTo": pay_to, "maxTimeoutSeconds": settings.x402_max_timeout_s, "extra": {"feePayer": facilitator.fee_payer()}}


def payment_required(req: dict, url: str, error: str = "payment required") -> dict:
    return {"x402Version": 2, "error": error, "resource": {"url": url, "description": "Sereel strategy data feed", "mimeType": "application/json"},
            "accepts": [req]}


def b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def decode_payment(header: str) -> dict:
    """The PAYMENT-SIGNATURE header: base64 JSON. Anything else is PAYMENT_INVALID."""
    try:
        obj = json.loads(base64.b64decode(header, validate=True))
    except (ValueError, TypeError):
        raise invalid() from None
    if not isinstance(obj, dict) or obj.get("x402Version") != 2 or not isinstance(obj.get("accepted"), dict) \
            or not isinstance((obj.get("payload") or {}).get("transaction"), str):
        raise invalid()
    return obj


def matches(accepted: dict, req: dict) -> bool:
    """The buyer must have signed for exactly what we ask: same scheme, network, asset, recipient and amount."""
    return all(accepted.get(k) == req[k] for k in ("scheme", "network", "asset", "payTo", "amount"))


# ---- the customer's token account ----------------------------------------------------------------------------------------------------------

def x402_ata(owner: str) -> Pubkey:
    return get_associated_token_address(Pubkey.from_string(owner), Pubkey.from_string(settings.x402_usdc_mint))


def funding_sol_text() -> str:
    try:
        return f"{sol.sol_balance(sol.funding_kp().pubkey()):.4f} SOL"
    except Exception:
        return "unknown"


def ensure_token_account(owner: str) -> tuple[str, str | None]:
    """Make sure `owner` has a token account for the x402 mint; the rent comes from the funding wallet. Idempotent: returns
    (address, creation signature or None if it already existed). An off-curve owner (a multisig vault) is allowed. Raises
    CHAIN_UNAVAILABLE naming the funding wallet's SOL balance, so a feed is never enabled with a pay_to that cannot be settled to."""
    ata = x402_ata(owner)
    try:
        info = sol.rpc("getAccountInfo", [str(ata), {"encoding": "base64", "commitment": "confirmed"}])
        if info and info.get("value"):
            return str(ata), None
        funding = sol.funding_kp()
        ix = create_idempotent_associated_token_account(funding.pubkey(), Pubkey.from_string(owner), Pubkey.from_string(settings.x402_usdc_mint))
        sig = sol.send([ix], [funding])
        log.info("created the x402 token account %s for %s (%s)", ata, owner, sig)
        return str(ata), sig
    except ServiceError:
        raise
    except Exception as e:
        raise ServiceError("CHAIN_UNAVAILABLE", f"could not create the customer's USDC token account for the data feed ({type(e).__name__}). The funding "
                           f"wallet holds {funding_sol_text()} of devnet SOL; each token account needs about 0.002 SOL of rent. Nothing was enabled.", 503) from e
