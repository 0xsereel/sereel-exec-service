"""Optional extra signals. Each function returns (value_dict, None) or (None, reason); a failure NEVER raises into the snapshot, so
the core cycle works with none of them. (PAXG/Oro via Jupiter was dropped: no mint or endpoint could be confirmed in the
timebox, and a guessed mint would price the wrong token.)"""
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import yaml

from .. import pyth
from ..config import ROOT, settings
from . import hl_readonly as hl

EVENTS_PATH = ROOT / "data" / "events.yaml"
WINDOW_H = 24


def load_events(path=None) -> list[dict]:
    return yaml.safe_load((path or EVENTS_PATH).read_text()) or []


def _event_window(e: dict) -> tuple[datetime, datetime]:
    tz = ZoneInfo(e.get("tz") or "UTC")
    day = datetime.strptime(str(e["date"]), "%Y-%m-%d")
    if e.get("time"):
        hh, mm = str(e["time"]).split(":")
        start = day.replace(hour=int(hh), minute=int(mm), tzinfo=tz)
        return start.astimezone(timezone.utc), start.astimezone(timezone.utc)
    start = day.replace(tzinfo=tz)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def calendar(now: datetime, events: list[dict] | None = None) -> tuple[dict | None, str | None]:
    """Verified events starting within the next 24h (or in progress today), and the next upcoming one."""
    try:
        events = load_events() if events is None else events
        soon, upcoming = [], []
        for e in events:
            start, end = _event_window(e)
            if end >= now and start <= now + timedelta(hours=WINDOW_H):
                soon.append(e)
            elif start > now:
                upcoming.append((start, e))
        nxt = min(upcoming, key=lambda t: t[0], default=None)
        return {"within_24h": [e["name"] for e in soon],
                "next_event": (f"{nxt[1]['name']} on {nxt[1]['date']}" if nxt else "none listed"),
                "source": "data/events.yaml (federalreserve.gov, bls.gov)"}, None
    except Exception as e:
        return None, f"calendar unavailable: {type(e).__name__}"


def pyth_history(feed_id: str, now_s: int | None = None) -> tuple[dict | None, str | None]:
    """Pyth prices 1h, 24h and 7d ago from Hermes' historical endpoint (the Benchmarks host answered 401/404 without credentials
    when probed, so it is not used)."""
    now_s = now_s or int(time.time())
    out = {}
    try:
        for name, ago in (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400)):
            r = httpx.get(f"{settings.pyth_hermes_url}/v2/updates/price/{now_s - ago}",
                          params={"ids[]": feed_id.removeprefix("0x"), "parsed": "true"}, headers=pyth._headers(), timeout=10)
            if r.status_code != 200:
                return None, f"pyth history HTTP {r.status_code}"
            p = r.json()["parsed"][0]["price"]
            out[name] = Decimal(p["price"]) * Decimal(10) ** int(p["expo"])
        return {k: v for k, v in out.items()}, None
    except Exception as e:
        return None, f"pyth history unavailable: {type(e).__name__}"


MAX_GOLD_DEVIATION = Decimal("0.015")  # another venue's gold mark is context only while within 1.5% of Pyth
PAXG_REASON = "mint not verified"
PREDICTED_REASON = "HIP-3 market not covered by predictedFundings"


def cross_venue(coin: str, pyth_px: Decimal | None) -> tuple[dict | None, str | None]:
    """Cross-venue context for gold, never a trigger for any decision:
    - funding: predictedFundings has no entry for HIP-3 markets such as xyz:GOLD, so the reference is the native PAXG perp's
      entry (per venue, normalised to a per-hour rate), only if PAXG is listed in mainnet meta;
    - other HIP-3 gold markets: included only when their mark is within 1.5% of the Pyth XAU price; the rest are returned in
      `excluded` (with mark and reason) so they can be shown to people but are never rendered into the text Jev reads."""
    try:
        paxg = {}
        if any(u["name"] == "PAXG" for u in hl.info({"type": "meta"})["universe"]):  # one meta read; skipped if PAXG is not listed
            for name, venues in hl.predicted_fundings():
                if name == "PAXG":
                    paxg = {v: Decimal(d["fundingRate"]) / Decimal(d.get("fundingIntervalHours") or 1) for v, d in venues if d}
        base = coin.split(":")[-1]
        included, excluded = {}, []
        for dex in hl.perp_dexs():
            name = f"{dex}:{base}"
            if name == coin:
                continue
            try:
                c = hl.asset_ctx(name, dex)
            except hl.SignalsReadError:
                continue
            mark, funding = Decimal(c["markPx"]), Decimal(c["funding"])
            if pyth_px and abs(mark - pyth_px) / pyth_px <= MAX_GOLD_DEVIATION:
                included[name] = {"mark": mark, "funding": funding}
            else:
                excluded.append({"market": name, "mark": mark, "reason": "outside 1.5% of Pyth" if pyth_px else "no Pyth price to compare"})
        return {"paxg_funding_hourly": paxg, "included": included, "excluded": excluded}, None
    except Exception as e:
        return None, f"cross-venue reads unavailable: {type(e).__name__}"
