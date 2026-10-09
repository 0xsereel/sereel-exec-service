"""Cantina's side of the data feed (X-Sereel-Key): configure it (owner-signed), see what a buyer would get, read the income ledger, and
publish NAV checkpoints (owner-signed)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Request
from sqlmodel import Session

from .. import auth as authmod
from .. import solana_client as sol
from ..config import settings
from ..db import engine
from ..deps import auth
from ..errors import ServiceError
from ..models import DataFeed, NavCheckpoint, now
from ..strategies import service
from . import custody, feed, requirements
from .router import endpoint_url

router = APIRouter(prefix="/strategies", dependencies=auth)
ISO_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
CONFIG_FIELDS = ("enabled", "price_usd", "pay_to", "fields", "fund_address")
FUTURE_SKEW_S = 30


def _bad(msg: str) -> ServiceError:
    return ServiceError("BAD_REQUEST", msg, 400)


async def _body(request: Request) -> tuple[dict, dict | None]:
    try:
        body = await request.json()
    except ValueError:
        raise _bad("request body must be JSON")
    if not isinstance(body, dict):
        raise _bad("request body must be a JSON object")
    return body, body.get("authorization")


def feed_out(sid: str, request: Request) -> dict:
    cfg = feed.config(sid)
    total, count, last = feed.income(sid)
    return {"enabled": bool(cfg and cfg.enabled), "price_usd": cfg.price_usd if cfg else None, "pay_to": cfg.pay_to if cfg else None,
            "fields": cfg.fields if cfg and cfg.fields else None, "endpoint_url": endpoint_url(request, sid),
            "total_income_usd": feed.money_str(total), "payment_count": count, "last_paid_at": feed.iso(last), "usdc_mint": settings.x402_usdc_mint}


@router.get("/{sid}/data-feed")
def get_feed(sid: str, request: Request):
    service.get_strategy(sid)
    return feed_out(sid, request)


@router.get("/{sid}/data-feed/preview")
def preview(sid: str):
    """Exactly the JSON a paying buyer would receive right now (no payment). Disabled means a buyer would get a 404, so this does too."""
    service.get_strategy(sid)
    cfg = feed.active_config(sid)
    if cfg is None:
        raise ServiceError("DATA_FEED_DISABLED", "no data feed is available for this id", 404)
    return feed.build(sid, cfg.fields.split(","))


@router.get("/{sid}/data-feed/payments")
def get_payments(sid: str, limit: int = 50):
    service.get_strategy(sid)
    return feed.payments(sid, limit)


@router.post("/{sid}/data-feed")
async def configure(sid: str, request: Request):
    """Owner-signed `configure_data_feed`. Flat string fields: enabled ("true"/"false"), price_usd, pay_to (default: the owner's wallet),
    fields (comma-separated from nav_per_share, nav_history, hedge_summary, attestations; default all four), optional fund_address.
    Disabled by default: a strategy exposes nothing until its owner enables it."""
    body, authorization = await _body(request)
    params = {k: body[k] for k in CONFIG_FIELDS if k in body}
    authmod.validate_params(params)
    st = service.get_strategy(sid)
    if params.get("enabled") not in ("true", "false"):
        raise _bad('enabled must be "true" or "false"')
    enabling = params["enabled"] == "true"
    prev = feed.config(sid)
    price = params.get("price_usd") or (prev.price_usd if prev else None)
    pay_to = params.get("pay_to") or (prev.pay_to if prev else None) or st.owner_pubkey
    fields = params.get("fields") or (prev.fields if prev and prev.fields else ",".join(feed.SELLABLE))
    fund_address = params.get("fund_address") or (prev.fund_address if prev else None)
    if enabling:
        if not price:
            raise _bad("price_usd is required to enable the feed")
        requirements.check_price(price)
        if not pay_to:
            raise _bad("pay_to is required: this strategy's owner is a multisig, so there is no default wallet to receive payments")
        if not sol.is_valid_address(pay_to):
            raise _bad("pay_to is not a valid Solana address")
        fields = ",".join(feed.parse_fields(fields))
    if fund_address and not sol.is_valid_address(fund_address):
        raise _bad("fund_address is not a valid Solana address")
    st, signer = service.authorize_action(sid, "configure_data_feed", authorization, params)  # the owner only: delegates are refused
    ata = created = None
    if enabling:
        ata, created = requirements.ensure_token_account(pay_to)  # refuses the enable if the customer's token account cannot be made
    with Session(engine) as s:
        row = s.get(DataFeed, sid) or DataFeed(strategy_id=sid)
        row.enabled, row.price_usd, row.pay_to, row.fields, row.fund_address, row.updated_at = enabling, price, pay_to, fields, fund_address, now()
        s.add(row)
        s.commit()
    record = {"event": "configure_data_feed", "enabled": enabling, "price_usd": price, "pay_to": pay_to, "fields": fields, "fund_address": fund_address,
              "token_account": ata, "token_account_created": created, "signed_by": signer, "authorization_nonce": (authorization or {}).get("nonce")}
    service._record_action(sid, "configure_data_feed", record, st.fund_id, signed_by=signer, authorization=authorization)
    return feed_out(sid, request)


# ---- NAV ----------------------------------------------------------------------------------------------------------------------------------

@router.post("/{sid}/nav")
async def publish_nav(sid: str, request: Request):
    """Owner-signed `publish_nav` {nav_per_share, unhedged_nav_per_share, as_of}: Cantina calls it right after writing a NAV checkpoint. The
    service stores it, attests it on Solana, and serves it as nav_per_share / nav_history. `as_of` may not be older than the latest stored
    checkpoint nor more than 30 s in the future; both NAVs must be positive decimals."""
    body, authorization = await _body(request)
    keys = ("nav_per_share", "unhedged_nav_per_share", "as_of")
    params = {k: body[k] for k in keys if k in body}
    missing = [k for k in keys if k not in params]
    if missing:
        raise _bad(f"missing: {', '.join(missing)}")
    authmod.validate_params(params)
    st = service.get_strategy(sid)
    for k in ("nav_per_share", "unhedged_nav_per_share"):
        if Decimal(params[k]) <= 0:
            raise _bad(f"{k} must be positive")
    if not ISO_RE.match(params["as_of"]):
        raise _bad('as_of must be an ISO 8601 UTC time such as "2026-10-15T09:00:00Z"')
    try:
        as_of = datetime.strptime(params["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise _bad("as_of is not a real date")
    if as_of > now() + timedelta(seconds=FUTURE_SKEW_S):
        raise _bad(f"as_of is more than {FUTURE_SKEW_S} seconds in the future")
    latest = feed.latest_checkpoint(sid)
    if latest is not None and as_of < latest.as_of:
        raise _bad(f"as_of {params['as_of']} is older than the latest published checkpoint ({feed.iso(latest.as_of)})")
    st, signer = service.authorize_action(sid, "publish_nav", authorization, params)  # the owner only
    with Session(engine) as s:
        cp = NavCheckpoint(strategy_id=sid, nav_per_share=params["nav_per_share"], unhedged_nav_per_share=params["unhedged_nav_per_share"], as_of=as_of,
                           published_by=signer)
        s.add(cp)
        s.commit()
        s.refresh(cp)
        cp_id = cp.id
    with Session(engine) as s:  # the proof is made only now that the checkpoint exists, and is kept OUT of the attested record and memo
        cp = s.get(NavCheckpoint, cp_id)
        cp.custody_proof = custody.generate(sid, feed.iso(cp.as_of), feed.iso(cp.created_at))
        s.add(cp)
        s.commit()
    record = {"event": "publish_nav", **params, "signed_by": signer, "authorization_nonce": (authorization or {}).get("nonce"), "checkpoint_id": cp_id}
    asig = service._record_action(sid, "publish_nav", record, st.fund_id, signed_by=signer, authorization=authorization)
    with Session(engine) as s:
        cp = s.get(NavCheckpoint, cp_id)
        cp.attestation_sig = asig
        s.add(cp)
        s.commit()
    return {"nav_per_share": params["nav_per_share"], "unhedged_nav_per_share": params["unhedged_nav_per_share"], "as_of": params["as_of"],
            "attestation_sig": asig}
