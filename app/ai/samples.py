"""price_samples: one reading per source per minute (volatility fallback when candle reads fail). Kept 14 days."""
import logging
from datetime import timedelta
from decimal import Decimal

from sqlmodel import Session, delete

from .. import pyth
from ..config import load_markets
from ..db import engine
from ..models import PriceSample, now
from . import hl_readonly as hl

log = logging.getLogger("sereel.samples")
KEEP = timedelta(days=14)


def sample_once() -> int:
    rows = []
    for m in load_markets().values():
        if not m.enabled:
            continue
        try:
            rows.append(PriceSample(market_id=m.market_id, source="pyth", price=pyth.get_price(m.pyth_feed_id, None).price))
        except Exception as e:
            log.debug("pyth sample skipped: %s", type(e).__name__)
        try:
            rows.append(PriceSample(market_id=m.market_id, source="hl_signals", price=Decimal(hl.asset_ctx(m.hl_coin, m.hl_dex)["markPx"])))
        except Exception as e:
            log.debug("hl sample skipped: %s", type(e).__name__)
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.exec(delete(PriceSample).where(PriceSample.ts < now() - KEEP))
        s.commit()
    return len(rows)


def register(sched) -> None:
    def tick():
        try:
            sample_once()
        except Exception:
            log.exception("price sample tick failed (will retry)")

    sched.add_job(tick, "interval", seconds=60, id="price-samples", max_instances=1, coalesce=True, misfire_grace_time=30)
