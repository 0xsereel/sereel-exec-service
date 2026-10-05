"""Pyth Hermes prices for display and sanity checks."""
import time
from dataclasses import dataclass
from decimal import Decimal

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

    @property
    def age_s(self) -> float:
        return time.time() - self.publish_time


def _headers() -> dict:
    return {"x-api-key": settings.pyth_api_key} if settings.pyth_api_key else {}


def get_price(feed_id: str, max_staleness_s: int | None = 30) -> PythPrice:
    """Latest price; raises STALE_PRICE if older than max_staleness_s (None disables the check)."""
    fid = feed_id.removeprefix("0x")
    if settings.pyth_mock_price is not None:
        return PythPrice(settings.pyth_mock_price, int(time.time()), fid)
    try:
        r = httpx.get(f"{settings.pyth_hermes_url}/v2/updates/price/latest",
                      params={"ids[]": fid, "parsed": "true"}, headers=_headers(), timeout=10)
    except httpx.HTTPError as e:
        raise PriceError("STALE_PRICE", f"Pyth unreachable: {e}", 503)
    if r.status_code in (401, 403):
        raise PriceError("STALE_PRICE", f"Pyth Hermes rejected the request ({r.status_code}); check PYTH_API_KEY", 503)
    r.raise_for_status()
    parsed = r.json().get("parsed") or []
    if not parsed:
        raise PriceError("UNKNOWN_MARKET", f"no Pyth feed {fid}", 404)
    p = parsed[0]["price"]
    out = PythPrice(Decimal(p["price"]) * Decimal(10) ** int(p["expo"]), int(p["publish_time"]), fid)
    if max_staleness_s is not None and out.age_s > max_staleness_s:
        raise PriceError("STALE_PRICE", f"Pyth price is {out.age_s:.0f}s old (max {max_staleness_s}s)", 503)
    return out


def search_feeds(query: str, asset_type: str | None = None) -> list[dict]:
    params = {"query": query}
    if asset_type:
        params["asset_type"] = asset_type
    r = httpx.get(f"{settings.pyth_hermes_url}/v2/price_feeds", params=params, headers=_headers(), timeout=10)
    r.raise_for_status()
    return [{"id": f["id"], "symbol": f["attributes"].get("symbol"), "type": f["attributes"].get("asset_type")} for f in r.json()]
