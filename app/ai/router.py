"""POST /agent/chat, GET /agent/chat/{id}. X-Sereel-Key like every other route. Cantina supplies the owner's funds and wallets in
`context`: the service does not hold them, so they are validated as data (types, sizes, caps) and never trusted as instructions."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from pydantic import BaseModel, StrictStr, field_validator

from .. import solana_client as sol
from ..config import settings
from ..deps import auth
from ..errors import ServiceError
from ..strategies import service
from . import chat, decisions, loop

router = APIRouter(prefix="/agent", dependencies=auth)


def _decimal(v: str) -> str:
    try:
        d = Decimal(v)
    except InvalidOperation:
        raise ValueError(f"'{v[:30]}' is not a decimal number")
    if not d.is_finite() or d < 0:
        raise ValueError("must be a non-negative number")
    return v


class Exposure(BaseModel):
    asset: StrictStr
    units: StrictStr

    @field_validator("asset")
    @classmethod
    def _a(cls, v):
        if not 1 <= len(v) <= 16:
            raise ValueError("asset must be 1-16 characters")
        return v

    @field_validator("units")
    @classmethod
    def _u(cls, v):
        return _decimal(v)


class Fund(BaseModel):
    fund_id: StrictStr
    name: StrictStr
    nav_usd: StrictStr | None = None
    shares: StrictStr | None = None
    exposures: list[Exposure] = []

    @field_validator("fund_id")
    @classmethod
    def _id(cls, v):
        if not 1 <= len(v) <= 64:
            raise ValueError("fund_id must be 1-64 characters")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if not 1 <= len(v) <= 100:
            raise ValueError("name must be 1-100 characters")
        return v

    @field_validator("nav_usd", "shares")
    @classmethod
    def _n(cls, v):
        return None if v is None else _decimal(v)

    @field_validator("exposures")
    @classmethod
    def _e(cls, v):
        if len(v) > 10:
            raise ValueError("at most 10 exposures per fund")
        return v


class Wallet(BaseModel):
    address: StrictStr
    label: StrictStr
    usdc_balance: StrictStr

    @field_validator("address")
    @classmethod
    def _addr(cls, v):
        if not sol.is_valid_address(v):
            raise ValueError("not a valid Solana address")
        return v

    @field_validator("label")
    @classmethod
    def _label(cls, v):
        if not 1 <= len(v) <= 60:
            raise ValueError("label must be 1-60 characters")
        return v

    @field_validator("usdc_balance")
    @classmethod
    def _b(cls, v):
        return _decimal(v)


class Context(BaseModel):
    owner_pubkey: StrictStr
    funds: list[Fund] = []
    wallets: list[Wallet] = []
    strategy_id: StrictStr | None = None

    @field_validator("owner_pubkey")
    @classmethod
    def _owner(cls, v):
        if not sol.is_valid_address(v):
            raise ValueError("not a valid Solana address")
        return v

    @field_validator("funds")
    @classmethod
    def _funds(cls, v):
        if len(v) > 20:
            raise ValueError("at most 20 funds")
        return v

    @field_validator("wallets")
    @classmethod
    def _wallets(cls, v):
        if len(v) > 10:
            raise ValueError("at most 10 wallets")
        return v

    @field_validator("strategy_id")
    @classmethod
    def _sid(cls, v):
        if v is not None and not 1 <= len(v) <= 64:
            raise ValueError("strategy_id must be 1-64 characters")
        return v


class ChatIn(BaseModel):
    session_id: StrictStr | None = None
    message: StrictStr
    context: Context


@router.post("/chat")
def post_chat(body: ChatIn):
    return chat.handle_message(body.session_id, body.message, body.context.model_dump())


@router.get("/chat/{session_id}")
def get_chat(session_id: str):
    return chat.get_session(session_id)


# ---- status, decisions, run-once ----------------------------------------------------------------------------------------------

@router.get("/status")
def status():
    last = loop.last_cycle["at"] or decisions.last_cycle_at()
    return {"enabled": settings.agent_enabled, "signals_network": settings.signals_source_network,
            "execution_network": "testnet" if settings.is_hl_testnet else "mainnet", "interval_s": settings.agent_interval_s,
            "last_cycle_at": decisions.iso(last), "jev_model": settings.jev_model if settings.jev_api_key else None,
            "llm_model": settings.llm_model if settings.llm_api_key else None, "agent_pubkey": None}


async def _signed(request: Request) -> tuple[dict, dict | None]:
    try:
        body = await request.json()
    except ValueError:
        raise ServiceError("BAD_REQUEST", "request body must be JSON", 400)
    if not isinstance(body, dict):
        raise ServiceError("BAD_REQUEST", "request body must be a JSON object", 400)
    return body, body.get("authorization")


@router.post("/run-once")
async def run_once(request: Request):
    """Owner-signed (`run_once`, params {strategy_id}): run one cycle now. Returns the AgentDecision, including `none`. A second call
    on the same strategy within 30 s returns the latest decision and spends no Jev or model call."""
    body, authorization = await _signed(request)
    sid = body.get("strategy_id")
    if not isinstance(sid, str) or not sid:
        raise ServiceError("BAD_REQUEST", "strategy_id is required", 400)
    if not settings.agent_enabled:
        raise ServiceError("AGENT_DISABLED", "the AI agent is switched off (AGENT_ENABLED=false)", 503)
    params = {"strategy_id": sid}
    from .. import auth as authmod

    authmod.validate_params(params)
    st = service.get_strategy(sid)
    if st.status != "active":
        raise ServiceError("CONFLICT", f"strategy is {st.status}; the agent only watches active strategies", 409)
    service.authorize_action(sid, "run_once", authorization, params)
    return decisions.out(loop.run_once(sid))


strategy_router = APIRouter(prefix="/strategies", dependencies=auth)


@strategy_router.get("/{sid}/agent/decisions")
def list_decisions(sid: str, limit: int = 50):
    service.get_strategy(sid)
    return decisions.list_for(sid, limit)


@strategy_router.post("/{sid}/agent/decisions/{did}/dismiss")
async def dismiss(sid: str, did: str, request: Request):
    """Owner-signed (`dismiss_decision`, params {decision_id}). Dismissing a decision that is not waiting changes nothing."""
    body, authorization = await _signed(request)
    if "decision_id" in body and body["decision_id"] != did:
        raise ServiceError("BAD_REQUEST", "decision_id in the body does not match the URL", 400)
    from .. import auth as authmod

    params = {"decision_id": did}
    authmod.validate_params(params)
    service.get_strategy(sid)
    if decisions.dismiss_lookup(sid, did) is None:
        raise ServiceError("NOT_FOUND", "decision not found", 404)
    service.authorize_action(sid, "dismiss_decision", authorization, params)
    return decisions.out(decisions.dismiss(sid, did))
