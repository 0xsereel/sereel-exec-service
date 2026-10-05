import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlmodel import func, select
from starlette.exceptions import HTTPException  # the base class: also covers unknown-route 404s and 405s

from . import solana_client as sol
from .config import load_markets, settings
from .db import init_db, session
from .deps import auth, require_key  # noqa: F401  (re-exported)
from .errors import ServiceError
from .models import S_ACTIVE, Schedule, Strategy

VERSION = "0.1.0"
log = logging.getLogger("sereel")


class State:
    def __init__(self):
        self.markets = load_markets()
        self.venue = None


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.assert_network_safe()
    init_db()
    from .payments import scheduler
    from .venue.hyperliquid import get_venue

    state.venue = get_venue(state.markets)
    sched = scheduler.start()
    try:
        yield
    finally:
        sched.shutdown(wait=False)


app = FastAPI(title="Sereel Execution Service", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                   allow_methods=["*"], allow_headers=["*"])


def err(status: int, code: str, message: str) -> JSONResponse:
    """Error body that satisfies both parser shapes: v4's {"error": <message>} and the original spec's
    {"code", "message"}. `error` and `message` carry the same text; `code` is the machine-readable code."""
    return JSONResponse(status_code=status, content={"error": message, "code": code, "message": message})


@app.exception_handler(ServiceError)
async def _service_error(_: Request, exc: ServiceError):
    return err(exc.status, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, exc: RequestValidationError):
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"] if p != "body")
    return err(400, "BAD_REQUEST", f"{where}: {first['msg']}" if where else first["msg"])


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException):
    return err(exc.status_code, "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR", str(exc.detail))


@app.exception_handler(Exception)
async def _unexpected(_: Request, exc: Exception):
    log.exception("unhandled error")
    return err(500, "INTERNAL", "unexpected error")


from .payments.router import router as payments_router  # noqa: E402

app.include_router(payments_router)


@app.get("/health")
def health():
    out = {"version": VERSION, "venue": settings.venue,
           "hyperliquid": {"network": "testnet" if settings.is_hl_testnet else "mainnet"},
           "solana": {"network": "devnet" if sol.is_devnet() else "other"},
           "stablecoin_mint": settings.stablecoin_mint or None}
    try:
        out["funding_address"] = str(sol.funding_kp().pubkey())
    except FileNotFoundError:
        out["funding_address"] = None
    if state.venue is not None:
        hl = out["hyperliquid"]
        hl["mode"] = getattr(state.venue, "mode", "simulated")
        try:
            hl["margin"] = float(state.venue.margin_balance(next(iter(state.markets))))
        except Exception as e:  # health must not fail because the venue is down
            hl["error"] = str(e)
    with session() as s:
        out["active_strategies"] = s.exec(select(func.count()).select_from(Strategy).where(Strategy.status == S_ACTIVE)).one()
        out["active_schedules"] = s.exec(select(func.count()).select_from(Schedule).where(Schedule.status == "active")).one()
    return out
