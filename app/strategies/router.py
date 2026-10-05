from fastapi import APIRouter, BackgroundTasks, Header, Request

from .. import auth as authmod
from .. import solana_client as sol
from ..config import settings
from ..deps import auth
from ..errors import ServiceError
from . import service, withdrawals

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


@router.patch("/{sid}")
async def edit(sid: str, request: Request, org: str = ORG):
    """Change the hedge ratio and/or exposure (moves the target only; rebalance trades). Signed: `edit_hedge_settings`.
    Both fields are strings and are exactly what is signed; a JSON number is rejected before anything is stored."""
    _, authorization, params = await signed_body(request, ("hedge_ratio_bps", "target_exposure_units"))
    authmod.validate_params(params)
    return service.strategy_out(service.edit_settings(sid, params, authorization, org))


@router.post("/{sid}/rebalance")
async def rebalance(sid: str, request: Request, force: bool = False, org: str = ORG):
    """Move the hedge to its target if the gap exceeds the rebalance band (`?force=true` trades regardless). Signed."""
    _, authorization, _ = await signed_body(request, ())
    return service.strategy_out(service.rebalance(sid, authorization, force, org))


@router.get("/{sid}/value")
def value(sid: str, as_of: str | None = None, org: str = ORG):
    """Live value, or the P&L snapshot at or just before `as_of` (ISO 8601 or Unix ms)."""
    return service.strategy_value(sid, org, as_of)


@router.get("/{sid}/history")
def history(sid: str, org: str = ORG):
    return service.strategy_history(sid, org)


@router.delete("/{sid}")
async def close(sid: str, request: Request, background: BackgroundTasks, org: str = ORG):
    """Close the strategy (signed: `close_strategy`). Returns a StrategyWithdrawal of type `close` at once; the position is
    closed, the funds released, bridged and paid to `destination_wallet_address` in the background. Poll
    GET /strategies/{id}/withdrawals/{wid}. Fails with NO_LIQUIDITY, sending nothing, if the book cannot take the close."""
    _, authorization, params = await signed_body(request, ("destination_wallet_address",))
    authmod.validate_params(params)
    w = withdrawals.request_close(sid, params, authorization, org)
    background.add_task(withdrawals.advance_withdrawal, w.id)
    return withdrawals.withdrawal_out(w)


@router.post("/{sid}/withdrawals")
async def withdraw(sid: str, request: Request, background: BackgroundTasks, org: str = ORG):
    """Return excess margin to a wallet while keeping the hedge open (signed: `return_excess`). `type` may be sent but only
    `return_excess` is accepted here; closing goes through DELETE /strategies/{id}."""
    body, authorization, params = await signed_body(request, ("amount_usd", "destination_wallet_address"))
    if body.get("type", "return_excess") != "return_excess":
        raise ServiceError("BAD_REQUEST", "type must be return_excess; use DELETE /strategies/{id} to close", 400)
    authmod.validate_params(params)
    w = withdrawals.request_excess(sid, params, authorization, org)
    background.add_task(withdrawals.advance_withdrawal, w.id)
    return withdrawals.withdrawal_out(w)


@router.get("/{sid}/withdrawals")
def list_withdrawals(sid: str, org: str = ORG):
    return [withdrawals.withdrawal_out(w) for w in withdrawals.list_withdrawals(sid, org)]


@router.get("/{sid}/withdrawals/{wid}")
def get_withdrawal(sid: str, wid: str, org: str = ORG):
    return withdrawals.withdrawal_out(withdrawals.get_withdrawal(sid, wid, org))
