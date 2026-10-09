"""The x402 facilitator (default: the public x402.org one): it verifies a signed payment and settles it on Solana. Settlement is a transfer
from the payer to the customer's wallet, with the facilitator paying the fee, so the service never receives or holds the money.

Wire format (x402 v2): POST /verify and POST /settle take {x402Version, paymentPayload, paymentRequirements}; /verify answers
{isValid, invalidReason?, payer?}, /settle {success, errorReason?, transaction, network, payer?}; GET /supported lists the kinds and the
fee payer. A transport failure or a 5xx is FACILITATOR_UNAVAILABLE (never PAYMENT_INVALID: the payer did nothing wrong)."""
import logging
import time

import httpx

from ..config import settings
from ..errors import ServiceError

log = logging.getLogger("sereel.x402")
_supported: dict = {"at": 0.0, "fee_payer": None}


def unavailable(why: str) -> ServiceError:
    return ServiceError("FACILITATOR_UNAVAILABLE", f"the payment facilitator is unavailable ({why}); you were not charged, try again shortly", 503)


def call(path: str, body: dict | None = None) -> dict:
    """One facilitator request. Always bounded by a timeout. Raises FACILITATOR_UNAVAILABLE on transport errors and 5xx."""
    url = settings.x402_facilitator_url.rstrip("/") + path
    try:
        r = httpx.post(url, json=body, timeout=settings.x402_timeout_s) if body is not None else httpx.get(url, timeout=settings.x402_timeout_s)
    except httpx.HTTPError as e:
        raise unavailable(type(e).__name__) from e
    if r.status_code >= 500:
        raise unavailable(f"HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise unavailable("not JSON") from None
    if not isinstance(data, dict):
        raise unavailable("unexpected response")
    return data


def fee_payer() -> str:
    """The facilitator's fee payer for our network (it must be in the transaction's fee-payer slot). Cached for ten minutes."""
    if _supported["fee_payer"] and time.time() - _supported["at"] < 600:
        return _supported["fee_payer"]
    data = call("/supported")
    for k in data.get("kinds", []):
        if k.get("scheme") == "exact" and k.get("network") == settings.x402_network and k.get("x402Version") == 2:
            fp = (k.get("extra") or {}).get("feePayer")
            if fp:
                _supported.update(at=time.time(), fee_payer=fp)
                return fp
    raise unavailable(f"it does not list the `exact` scheme on {settings.x402_network}")


def verify(payload: dict, requirements: dict) -> dict:
    return call("/verify", {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements})


def settle(payload: dict, requirements: dict) -> dict:
    return call("/settle", {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements})
