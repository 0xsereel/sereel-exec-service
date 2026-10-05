from decimal import Decimal

from fastapi import APIRouter
from pydantic import BaseModel

from .. import solana_client as sol
from ..errors import ServiceError
from ..config import settings
from ..deps import auth
from ..models import Payment, Schedule
from ..util import iso, num
from . import service

router = APIRouter(prefix="/payments", dependencies=auth)


class SendIn(BaseModel):
    to: str
    amount_usd: Decimal
    memo: str = ""


class StatusIn(BaseModel):
    status: str  # active | paused


def schedule_out(s: Schedule) -> dict:
    return {"id": s.id, "name": s.name, "fund_id": s.fund_id, "to": s.to, "interval_seconds": s.interval_seconds,
            "amount_mode": s.amount_mode, "amount_usd": num(s.amount_usd), "pricing": s.pricing,
            "memo_template": s.memo_template, "max_payments": s.max_payments, "end_at": iso(s.end_at),
            "status": s.status, "payments_made": s.seq, "total_paid_usd": num(s.total_paid_usd),
            "next_run": iso(s.next_run), "created_at": iso(s.created_at)}


def payment_out(p: Payment) -> dict:
    return {"id": p.id, "schedule_id": p.schedule_id, "seq": p.seq, "to": p.to, "amount_usd": num(p.amount_usd),
            "memo": p.memo, "signature": p.signature, "status": p.status, "error": p.error, "created_at": iso(p.created_at)}


@router.get("/mint")
def mint():
    if not settings.stablecoin_mint:
        raise ServiceError("NOT_CONFIGURED", "STABLECOIN_MINT not set; run `sereel init`", 503)
    return {"mint": settings.stablecoin_mint, "decimals": sol.DECIMALS}


@router.post("/send")
def send(body: SendIn):
    return payment_out(service.send_payment(body.to, body.amount_usd, body.memo))


@router.post("/schedules")
def create(body: service.ScheduleIn):
    return schedule_out(service.create_schedule(body))


@router.get("/schedules")
def list_():
    return [schedule_out(s) for s in service.list_schedules()]


@router.patch("/schedules/{sid}")
def patch(sid: str, body: StatusIn):
    if body.status not in ("active", "paused"):
        raise ServiceError("BAD_REQUEST", "status must be 'active' or 'paused'", 400)
    return schedule_out(service.set_status(sid, body.status))


@router.delete("/schedules/{sid}")
def stop(sid: str):
    return schedule_out(service.set_status(sid, "stopped"))


@router.get("/history")
def history(to: str | None = None):
    return [payment_out(p) for p in service.list_payments(to)]
