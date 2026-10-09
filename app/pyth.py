"""Pyth Hermes prices for display and sanity checks."""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from .config import settings
from .errors import ServiceError


class PriceError(ServiceError):
    pass


@dataclass
class PythPrice:
    price: Decimal
    publish_time: int
    feed_id: str
    conf: Decimal = Decimal(0)  # Pyth's 1-sigma confidence interval, in the same units as the price
    market_closed: bool = False  # True: the price is stale because the market is closed (per the feed's schedule)

    @property
    def age_s(self) -> float:
        return time.time() - self.publish_time


def _headers() -> dict:
    return {"Authorization": f"Bearer {settings.pyth_api_key}"} if settings.pyth_api_key else {}


def _check_auth(r: httpx.Response) -> None:
    if r.status_code in (401, 403):
        raise PriceError("PRICE_SOURCE_AUTH", f"Pyth Hermes rejected the credentials ({r.status_code}); check PYTH_API_KEY", 502)


# ---- market hours -----------------------------------------------------------
# A feed's `schedule` attribute: "<tz>;<7 weekly entries Mon..Sun>;<holiday overrides>", e.g.
#   America/New_York;0000-1700&1800-2400,...,0000-1700,C,1800-2400;0907/0000-1430&1800-2400,1225/C
# An entry is "C" (closed all day) or "HHMM-HHMM" intervals joined by "&" (2400 = midnight). Holidays are MMDD/entry.
_SCHEDULE_TTL_S = 6 * 3600
_schedules: dict[str, tuple[float, str | None]] = {}


def _intervals(entry: str) -> list[tuple[int, int]]:
    if entry.strip().upper() == "C":
        return []
    out = []
    for part in entry.split("&"):
        a, b = part.split("-")
        out.append((int(a), int(b)))
    return out


def is_open(schedule: str, at: datetime) -> bool:
    tz_name, weekly, *rest = schedule.split(";")
    local = at.astimezone(ZoneInfo(tz_name))
    entry = weekly.split(",")[local.weekday()]  # Monday == 0
    if rest and rest[0]:
        for h in rest[0].split(","):
            day, _, hours = h.partition("/")
            if day == f"{local:%m%d}":
                entry = hours
                break
    hhmm = local.hour * 100 + local.minute
    return any(start <= hhmm < end for start, end in _intervals(entry))


def feed_schedule(feed_id: str, symbol: str) -> str | None:
    """The feed's market-hours schedule (cached for hours), or None if it cannot be determined."""
    fid = feed_id.removeprefix("0x")
    hit = _schedules.get(fid)
    if hit and time.time() - hit[0] < _SCHEDULE_TTL_S:
        return hit[1]
    schedule = None
    try:
        r = httpx.get(f"{settings.pyth_hermes_url}/v2/price_feeds", params={"query": symbol}, headers=_headers(), timeout=10)
        if r.status_code == 200:
            schedule = next((f["attributes"].get("schedule") for f in r.json() if f["id"].removeprefix("0x") == fid), None)
    except httpx.HTTPError:
        pass
    _schedules[fid] = (time.time(), schedule)
    return schedule


def get_price(feed_id: str, max_staleness_s: int | None = 30, symbol: str | None = None) -> PythPrice:
    """Latest price. If it is older than max_staleness_s (None disables the check) and `symbol` is given, the feed's
    schedule decides: market closed -> return the last price flagged market_closed=True; market open (or schedule
    unknown) -> STALE_PRICE. Also STALE_PRICE if Pyth is unreachable, PRICE_SOURCE_AUTH if the credentials are rejected."""
    fid = feed_id.removeprefix("0x")
    if settings.pyth_mock_price is not None:
        return PythPrice(settings.pyth_mock_price, int(time.time()), fid)
    try:
        r = httpx.get(f"{settings.pyth_hermes_url}/v2/updates/price/latest",
                      params={"ids[]": fid, "parsed": "true"}, headers=_headers(), timeout=10)
    except httpx.HTTPError as e:
        raise PriceError("STALE_PRICE", f"Pyth unreachable: {e}", 503)
    _check_auth(r)
    r.raise_for_status()
    parsed = r.json().get("parsed") or []
    if not parsed:
        raise PriceError("UNKNOWN_MARKET", f"no Pyth feed {fid}", 404)
    p = parsed[0]["price"]
    out = PythPrice(Decimal(p["price"]) * Decimal(10) ** int(p["expo"]), int(p["publish_time"]), fid,
                    conf=Decimal(p.get("conf", "0")) * Decimal(10) ** int(p["expo"]))
    if max_staleness_s is not None and out.age_s > max_staleness_s:
        schedule = feed_schedule(fid, symbol) if symbol else None
        if schedule and not is_open(schedule, datetime.now(timezone.utc)):
            out.market_closed = True  # last price from before the close; the market is not publishing
            return out
        raise PriceError("STALE_PRICE", f"Pyth price is {out.age_s:.0f}s old (max {max_staleness_s}s)", 503)
    return out


def search_feeds(query: str, asset_type: str | None = None) -> list[dict]:
    params = {"query": query}
    if asset_type:
        params["asset_type"] = asset_type
    r = httpx.get(f"{settings.pyth_hermes_url}/v2/price_feeds", params=params, headers=_headers(), timeout=10)
    _check_auth(r)
    r.raise_for_status()
    return [{"id": f["id"], "symbol": f["attributes"].get("symbol"), "type": f["attributes"].get("asset_type")} for f in r.json()]
