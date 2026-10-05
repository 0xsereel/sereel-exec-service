from fastapi import APIRouter, Header, Request

from .. import solana_client as sol
from ..config import settings
from ..deps import auth
from ..errors import ServiceError
from . import service

router = APIRouter(prefix="/strategies", dependencies=auth)

ORG = Header(default="", alias="X-Sereel-Org")
USER = Header(default="", alias="X-Sereel-User")


async def signed_body(request: Request, allowed: tuple[str, ...]) -> tuple[dict, dict | None, dict]:
    """(raw body, authorization, params). `params` is exactly the action's own fields as the client sent them: the
    signed hash covers those raw JSON values, so they are read from the raw body, not from a re-serialised model."""
    try:
        body = await request.json()
    except ValueError:
        raise ServiceError("BAD_REQUEST", "request body must be JSON", 400)
    if not isinstance(body, dict):
        raise ServiceError("BAD_REQUEST", "request body must be a JSON object", 400)
    return body, body.get("authorization"), {k: body[k] for k in allowed if k in body}


@router.get("/funding-address")  # registered before /{sid}, or "funding-address" would be read as an id
def funding_address():
    return {"address": service.funding_address(), "stablecoin_mint": settings.stablecoin_mint,
            "network": "devnet" if sol.is_devnet() else "mainnet"}


@router.post("")
def create(body: service.CreateStrategyIn, user: str = USER, org: str = ORG):
    return service.strategy_out(service.create_strategy(body, user, org))


@router.get("")
def list_(owner: str | None = None, fund_id: str | None = None, org: str = ORG):
    return [service.strategy_out(s) for s in service.list_strategies(owner, fund_id, org)]


@router.get("/{sid}")
def get(sid: str, org: str = ORG):
    return service.strategy_out(service.get_strategy(sid, org))


@router.post("/{sid}/cancel")
def cancel(sid: str, org: str = ORG):
    return service.strategy_out(service.cancel_strategy(sid, org))


@router.post("/{sid}/deposits")
def create_deposit(sid: str, body: service.DepositIn, org: str = ORG):
    return service.deposit_out(service.create_deposit(sid, body, org))


@router.get("/{sid}/deposits")
def list_deposits(sid: str, org: str = ORG):
    return [service.deposit_out(d) for d in service.list_deposits(sid, org)]


@router.get("/{sid}/deposits/{did}")
def get_deposit(sid: str, did: str, org: str = ORG):
    return service.deposit_out(service.get_deposit(sid, did, org))


@router.post("/{sid}/owner")
async def change_owner(sid: str, request: Request, org: str = ORG):
    """Change who may manage the strategy. Signed by the CURRENT owner (action `change_owner`; params are exactly the
    one of owner_pubkey / owner_multisig being set)."""
    _, authorization, params = await signed_body(request, ("owner_pubkey", "owner_multisig"))
    return service.strategy_out(service.change_owner(sid, params, authorization, org))
