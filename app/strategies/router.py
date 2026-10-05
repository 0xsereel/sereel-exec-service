from fastapi import APIRouter, Header

from .. import solana_client as sol
from ..config import settings
from ..deps import auth
from . import service

router = APIRouter(prefix="/strategies", dependencies=auth)

ORG = Header(default="", alias="X-Sereel-Org")
USER = Header(default="", alias="X-Sereel-User")


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
