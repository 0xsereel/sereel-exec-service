"""Read-only Hyperliquid `info` reads for market signals.

This module can only POST to `{base}/info`. It never builds an Exchange, never holds a key, and is deliberately separate from the
execution venue: signals come from SIGNALS_HL_URL (mainnet by default, because testnet gold has thin books and ~0% funding) while
every order still goes to HL_API_URL (testnet). A test checks it imports nothing that can sign and never touches `/exchange`.
"""
import httpx

from ..config import settings

TIMEOUT_S = 10.0


class SignalsReadError(Exception):
    pass


def info(body: dict, base: str | None = None) -> object:
    url = (base or settings.signals_url).rstrip("/") + "/info"
    try:
        r = httpx.post(url, json=body, timeout=TIMEOUT_S)
    except httpx.HTTPError as e:
        raise SignalsReadError(f"{body.get('type')}: {type(e).__name__}") from e
    if r.status_code != 200:
        raise SignalsReadError(f"{body.get('type')}: HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise SignalsReadError(f"{body.get('type')}: not JSON") from e


def asset_ctx(coin: str, dex: str, base: str | None = None) -> dict:
    meta, ctxs = info({"type": "metaAndAssetCtxs", "dex": dex}, base)
    for u, c in zip(meta["universe"], ctxs):
        if u["name"] == coin:
            return c
    raise SignalsReadError(f"{coin} not listed on dex '{dex}'")


def candles(coin: str, interval: str, start_ms: int, end_ms: int, base: str | None = None) -> list[dict]:
    return info({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}}, base)


def l2_book(coin: str, base: str | None = None) -> tuple[list[dict], list[dict]]:
    bids, asks = info({"type": "l2Book", "coin": coin}, base)["levels"]
    return bids, asks


def predicted_fundings(base: str | None = None) -> list:
    return info({"type": "predictedFundings"}, base)


def perp_dexs(base: str | None = None) -> list[str]:
    return [d["name"] for d in info({"type": "perpDexs"}, base) if d]


def dex_meta(dex: str, base: str | None = None) -> dict:
    return info({"type": "meta", "dex": dex}, base)
