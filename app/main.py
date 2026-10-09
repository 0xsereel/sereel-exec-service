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
from .db import check_db_at_head, init_db, session
from .deps import auth, require_key  # noqa: F401  (re-exported)
from .errors import ServiceError
from .models import S_ACTIVE, Schedule, Strategy
from .state import state

VERSION = "0.1.0"
log = logging.getLogger("sereel")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.assert_network_safe()
    settings.assert_auth_config_safe()
    if settings.auth_bypass_active:
        log.warning("=" * 78 + "\n  DEV_AUTH_BYPASS IS ON: signed-message authorization is NOT enforced. DEV ONLY.\n" + "=" * 78)
    if settings.migrate_on_start:
        init_db()
    else:
        check_db_at_head()  # production: migrations are run explicitly, never implicitly by the API
    from .payments import scheduler
    from .venue.hyperliquid import get_venue

    state.venue = get_venue(state.markets)
    from .strategies import service as strategies_service

    strategies_service.recover_interrupted()
    from .strategies import watcher

    try:
        watcher.ensure_cursor()  # baseline BEFORE any intent can exist, so no deposit can fall into the gap
    except Exception as e:  # RPC hiccup: the first watcher tick retries it
        log.warning("deposit watcher baseline deferred: %s", e)
    sched = scheduler.start()
    watcher.register(sched)
    try:
        yield
    finally:
        sched.shutdown(wait=False)


app = FastAPI(title="Sereel Execution Service", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                   allow_methods=["*"], allow_headers=["*"])


def err(status: int, code: str, message: str) -> JSONResponse:
    """Cantina v4 error body, exactly: {"error": <human message>, "code": <CODE>} (codes: app.errors.CODES)."""
    return JSONResponse(status_code=status, content={"error": message, "code": code})


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
from .strategies.router import router as strategies_router  # noqa: E402
from .ai.router import router as agent_router  # noqa: E402
from .ai.router import strategy_router as agent_strategy_router  # noqa: E402

app.include_router(payments_router)
app.include_router(strategies_router)
app.include_router(agent_router)
app.include_router(agent_strategy_router)


@app.get("/health")
def health():
    from .strategies import service as strategies
    from .strategies import watcher

    out = {"version": VERSION, "venue": settings.venue,
           "hyperliquid": {"network": "testnet" if settings.is_hl_testnet else "mainnet"},
           "solana": {"network": "devnet" if sol.is_devnet() else "other"},
           "stablecoin_mint": settings.stablecoin_mint or None}
    from .ai import agent_key

    try:
        out["agent_pubkey"] = agent_key.pubkey()  # what an owner grants a delegation to; null until the key exists
    except Exception:
        out["agent_pubkey"] = None
    try:
        out["funding_address"] = str(sol.funding_kp().pubkey())
    except FileNotFoundError:
        out["funding_address"] = None
    if state.venue is not None:
        hl = out["hyperliquid"]
        hl["mode"] = getattr(state.venue, "mode", "simulated")
        try:
            hl["margin"] = float(state.venue.margin_balance(next(iter(state.markets))))
            # leverage is asserted, not assumed: what the venue reports vs what markets.yaml configures
            hl["leverage"] = {}
            for mid, m in state.markets.items():
                actual = state.venue.leverage_status(mid) or {}
                hl["leverage"][mid] = {"configured": m.max_leverage, "actual": actual.get("leverage"), "mode": actual.get("mode"),
                                       "ok": actual.get("leverage") == m.max_leverage}
            out["reconciliation"] = strategies.reconcile()
        except Exception as e:  # health must not fail because the venue is down
            hl["error"] = str(e)
    with session() as s:
        out["active_strategies"] = s.exec(select(func.count()).select_from(Strategy).where(Strategy.status == S_ACTIVE)).one()
        out["active_schedules"] = s.exec(select(func.count()).select_from(Schedule).where(Schedule.status == "active")).one()
    out["unresolved_transfers"] = watcher.unresolved_transfers()  # refunds that failed or are unconfirmed: need a human
    from .strategies import withdrawals as strategy_withdrawals

    out["unresolved_withdrawals"] = strategy_withdrawals.unresolved()  # failed withdrawals that need an operator
    return out


# Last good prices per market. The frontend types every price as a non-null number, so a failed read must never become `null` in
# a row: it serves the last good values (flagged price_stale) for a while, and if there are none the whole call is a clear error.
_market_cache: dict[str, tuple[float, dict]] = {}
MARKET_CACHE_MAX_AGE_S = 600


def _live_prices(mid: str, m) -> dict:
    from . import pyth
    from .util import num

    mark = state.venue.mark_price(mid)
    q = pyth.get_price(m.pyth_feed_id, m.max_staleness_s, symbol=m.symbol)
    return {"mark_price_usd": num(mark), "pyth_price_usd": num(q.price), "market_closed": bool(q.market_closed),
            "deviation_bps": num(abs(mark - q.price) / q.price * 10_000)}


@app.get("/markets", dependencies=auth)
def markets():
    """Markets with live Hyperliquid mark and Pyth price. Every price field is always a number and market_closed always a boolean
    (the frontend's StrategyMarket type is non-nullable): on a failed read the last good values are served with
    `price_stale: true` and the reason in `error`; with no usable values at all the call fails with a clear 503 instead."""
    import time

    rows, failures = [], []
    for mid, m in state.markets.items():
        row = {"market_id": mid, "symbol": m.symbol, "status": m.status, "venue_coin": m.hl_coin, "max_leverage": m.max_leverage}
        try:
            prices = _live_prices(mid, m)
            _market_cache[mid] = (time.time(), prices)
            row.update(prices, price_stale=False)
        except Exception as e:  # noqa: BLE001
            failures.append(e)
            cached = _market_cache.get(mid)
            if cached is None or time.time() - cached[0] > MARKET_CACHE_MAX_AGE_S:
                continue  # nothing trustworthy to show for this market
            row.update(cached[1], price_stale=True, error=getattr(e, "message", str(e)))
        rows.append(row)
    if not rows and failures:
        e = failures[0]
        raise ServiceError(getattr(e, "code", "VENUE_UNAVAILABLE"), f"no market price is available right now: "
                           f"{getattr(e, 'message', str(e))}", getattr(e, "status", 503) if isinstance(e, ServiceError) else 503)
    return rows
