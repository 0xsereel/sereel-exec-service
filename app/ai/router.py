"""POST /agent/chat, GET /agent/chat/{id}. X-Sereel-Key like every other route. Cantina supplies the owner's funds and wallets in
`context`: the service does not hold them, so they are validated as data (types, sizes, caps) and never trusted as instructions."""
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter
from pydantic import BaseModel, StrictStr, field_validator

from .. import solana_client as sol
from ..deps import auth
from . import chat

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
