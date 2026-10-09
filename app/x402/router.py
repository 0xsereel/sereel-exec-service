"""The public, paid route: GET /x402/strategies/{id}. No X-Sereel-Key: anyone may call it, and it answers only to a verified, settled payment.

  unpaid        -> 402 with a PAYMENT-REQUIRED header (v2) built from the stored config: price, the customer's `pay_to`, Circle devnet USDC
  paid          -> verify, then settle through the facilitator, then 200 with the enabled fields and a PAYMENT-RESPONSE header
  disabled/unknown id -> the identical 404 DATA_FEED_DISABLED (nothing about a strategy leaks)

The service never receives the money: the payment is a transfer from the payer to `pay_to`. The requirements sent to the facilitator are
always OUR OWN, never the buyer's; the buyer's `accepted` must merely equal them."""
import hashlib
import logging
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from ..config import settings
from ..db import engine
from ..errors import ServiceError
from ..models import DataPayment
from . import facilitator, feed, requirements

log = logging.getLogger("sereel.x402")
public_router = APIRouter(prefix="/x402")
_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)
_seen: dict[str, float] = {}  # sha256 of a transaction payload -> when: a payment is handled once (the spec's duplicate-settlement guard)
SEEN_TTL_S = 120


def _limit(key: str) -> None:
    t = time.time()
    with _lock:
        q = _hits[key]
        while q and t - q[0] > 60:
            q.popleft()
        if len(q) >= settings.x402_rate_per_min:
            raise ServiceError("RATE_LIMITED", f"too many requests: at most {settings.x402_rate_per_min} per minute", 429)
        q.append(t)


def _claim(tx_b64: str) -> bool:
    """True the first time this exact transaction is seen in the last two minutes."""
    h, t = hashlib.sha256(tx_b64.encode()).hexdigest(), time.time()
    with _lock:
        for k in [k for k, v in _seen.items() if t - v > SEEN_TTL_S]:
            del _seen[k]
        if h in _seen:
            return False
        _seen[h] = t
        return True


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")) or "?"


def public_base(request: Request) -> str:
    if settings.public_url:
        return settings.public_url.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


def endpoint_url(request: Request, sid: str) -> str:
    return f"{public_base(request)}/x402/strategies/{sid}"


def _pay_headers(req: dict, url: str, error: str) -> dict:
    return {"PAYMENT-REQUIRED": requirements.b64(requirements.payment_required(req, url, error))}


@public_router.get("/strategies/{sid}")
def paid(sid: str, request: Request):
    cfg = feed.active_config(sid)
    if cfg is None:
        raise ServiceError("DATA_FEED_DISABLED", "no data feed is available for this id", 404)
    _limit("ip:" + client_ip(request))
    url = endpoint_url(request, sid)
    req = requirements.requirements(cfg.pay_to, cfg.price_usd)  # from our config; FACILITATOR_UNAVAILABLE if the fee payer cannot be learned
    header = request.headers.get("PAYMENT-SIGNATURE")
    if not header:
        return JSONResponse(status_code=402, content={}, headers=_pay_headers(req, url, "payment required"))
    again = _pay_headers(req, url, "payment was not accepted")
    try:
        payment = requirements.decode_payment(header)
        if not requirements.matches(payment["accepted"], req):
            log.info("x402 %s: the buyer signed for different terms", sid[:8])
            raise requirements.invalid(again)
        if not _claim(payment["payload"]["transaction"]):
            log.info("x402 %s: the same transaction was submitted twice", sid[:8])
            raise requirements.invalid(again)
        verified = facilitator.verify(payment, req)
        if not verified.get("isValid"):
            log.info("x402 %s: facilitator rejected the payment: %s", sid[:8], verified.get("invalidReason"))
            raise requirements.invalid(again)
        payer = verified.get("payer") or "unknown"
        _limit("payer:" + payer)
        settled = facilitator.settle(payment, req)
    except ServiceError as e:
        if e.code == "PAYMENT_INVALID" and e.headers is None:
            e.headers = again
        raise
    if not settled.get("success") or not settled.get("transaction"):
        log.info("x402 %s: settlement failed: %s", sid[:8], settled.get("errorReason"))
        raise requirements.invalid(again)
    try:
        with Session(engine) as s:
            s.add(DataPayment(strategy_id=sid, payer=settled.get("payer") or payer, amount_usd=_price(cfg.price_usd), mint=settings.x402_usdc_mint,
                              tx_signature=settled["transaction"], fields_served=cfg.fields))
            s.commit()
    except IntegrityError:  # this settlement was already recorded: the same payment never buys the data twice
        log.info("x402 %s: settlement %s was already recorded", sid[:8], settled["transaction"][:12])
        raise requirements.invalid(again) from None
    except Exception:
        log.exception("x402 %s: PAID but recording failed (tx %s): the buyer still gets the data", sid[:8], settled.get("transaction"))
    body = feed.build(sid, cfg.fields.split(","))
    return JSONResponse(status_code=200, content=body, headers={"PAYMENT-RESPONSE": requirements.b64(
        {"success": True, "transaction": settled["transaction"], "network": settled.get("network", settings.x402_network), "payer": settled.get("payer") or payer})})


def _price(text: str):
    from decimal import Decimal
    return Decimal(text)
