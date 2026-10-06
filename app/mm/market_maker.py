"""Testnet-only two-sided quoter, run from a separate Hyperliquid account (HL_MM_*).

- Quotes are centered on the oracle (default), Pyth, or the mark. Centering on the oracle pulls a drifted mark back:
  a quote that would cross the book is sent as GTC (it takes the stale liquidity, then rests); a quote that rests
  safely is post-only (ALO).
- Inventory control: quotes skew against current inventory, a side that would grow inventory past the limit is
  dropped, and on shutdown (Ctrl-C / SIGTERM / `sereel mm stop`) all quotes are cancelled and the position is
  flattened with a reduce-only IOC.
"""
import logging
import os
import signal
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from .. import pyth
from ..config import ROOT, Market, settings
from ..errors import ServiceError
from ..venue.hyperliquid import (account_mode, connect_with_retries, exchange_for, is_unified, shared_info,
                                  use_request_timeouts)

log = logging.getLogger("sereel.mm")

BANNER = "=" * 72 + "\n  SEREEL MARKET MAKER: TESTNET LIQUIDITY ONLY. NOT FOR MAINNET.\n" + "=" * 72
CENTERS = ("oracle", "pyth", "mark")


@dataclass
class MMConfig:
    market_id: str
    spread_bps: Decimal = Decimal(10)  # level n sits n * spread_bps from the (skewed) center
    levels: int = 3
    size: Decimal = Decimal("0.03")  # coin units per level, bounded by MM_MIN_SIZE..MM_MAX_SIZE
    refresh_s: float = 5
    center: str = "oracle"  # oracle | pyth | mark
    flatten_wait_s: float = 60  # on startup: how long to wait for the book to let leftover inventory be flattened
    max_inventory: Decimal | None = None  # coin units; default levels * size
    skew_bps: Decimal | None = None  # center shift at max inventory; default levels * spread_bps

    def __post_init__(self):
        if not settings.mm_min_size <= self.size <= settings.mm_max_size:
            raise ServiceError("MM_SIZE_OUT_OF_RANGE",
                               f"size {self.size} outside {settings.mm_min_size}..{settings.mm_max_size} (MM_MIN_SIZE/MM_MAX_SIZE)")
        if self.levels < 1 or self.spread_bps <= 0:
            raise ServiceError("MM_BAD_CONFIG", "levels must be >= 1 and spread_bps > 0")
        if self.center not in CENTERS:
            raise ServiceError("MM_BAD_CONFIG", f"center must be one of {CENTERS}")
        if self.max_inventory is None:
            self.max_inventory = self.levels * self.size
        if self.skew_bps is None:
            self.skew_bps = self.levels * self.spread_bps


def assert_testnet() -> None:
    """Hard guard, independent of ALLOW_MAINNET."""
    if "testnet" not in settings.hl_api_url:
        raise ServiceError("MM_MAINNET_REFUSED", f"market maker refuses to run: HL_API_URL={settings.hl_api_url} is not testnet")


def pidfile(market_id: str) -> Path:
    return ROOT / "profiles" / f".mm-{market_id}.pid"


def request_stop(market_id: str) -> bool:
    """`sereel mm stop`: ask a running market maker to shut down gracefully (it flattens on the way out)."""
    p = pidfile(market_id)
    if not p.exists():
        return False
    try:
        os.kill(int(p.read_text()), signal.SIGTERM)
        return True
    except (ProcessLookupError, ValueError):
        p.unlink(missing_ok=True)
        return False


class MarketMaker:
    def __init__(self, market: Market, cfg: MMConfig):
        assert_testnet()
        if not (settings.hl_mm_account_address and settings.hl_mm_api_wallet_key):
            raise ServiceError("VENUE_NOT_CONFIGURED", "HL_MM_ACCOUNT_ADDRESS / HL_MM_API_WALLET_KEY not set", 503)
        self.market, self.cfg = market, cfg
        self.addr = settings.hl_mm_account_address
        dexs = ["", market.hl_dex] if market.hl_dex else None
        self.info = shared_info(dexs)  # one metadata download, long connect timeout, retried (see app/venue/hyperliquid.py)
        self.exchange = exchange_for(settings.hl_mm_api_wallet_key, self.addr, self.info)
        meta = connect_with_retries("loading the market", lambda: self.info.meta(dex=market.hl_dex))
        self.sz_dec = next(int(u["szDecimals"]) for u in meta["universe"] if u["name"] == market.hl_coin)
        self.mode = connect_with_retries("reading the account mode", lambda: account_mode(self.info, self.addr))
        use_request_timeouts(self.info, self.exchange)
        self._stop = False

    # -- market data ----------------------------------------------------------
    def ctx(self) -> dict:
        meta, ctxs = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": self.market.hl_dex})
        return next(c for u, c in zip(meta["universe"], ctxs) if u["name"] == self.market.hl_coin)

    def mark(self) -> Decimal:
        return Decimal(self.ctx()["markPx"])

    def center_price(self) -> Decimal:
        if self.cfg.center == "pyth":
            return pyth.get_price(self.market.pyth_feed_id, self.market.max_staleness_s).price
        c = self.ctx()
        return Decimal(c["oraclePx"] if self.cfg.center == "oracle" else c["markPx"])

    def best_bid_ask(self) -> tuple[Decimal | None, Decimal | None]:
        bids, asks = self.info.post("/info", {"type": "l2Book", "coin": self.market.hl_coin})["levels"]
        return (Decimal(bids[0]["px"]) if bids else None, Decimal(asks[0]["px"]) if asks else None)

    def book_offers(self, is_buy: bool, limit: Decimal) -> Decimal:
        """Size the book offers to an order on this side up to `limit`: asks at or below it when buying, bids at or above it
        when selling (the market maker's own quotes are cancelled before this is asked)."""
        bids, asks = self.info.post("/info", {"type": "l2Book", "coin": self.market.hl_coin})["levels"]
        if is_buy:
            return sum((Decimal(l["sz"]) for l in asks if Decimal(l["px"]) <= limit), Decimal(0))
        return sum((Decimal(l["sz"]) for l in bids if Decimal(l["px"]) >= limit), Decimal(0))

    def margin(self) -> Decimal:
        return Decimal(self.info.user_state(self.addr, dex=self.market.hl_dex)["marginSummary"]["accountValue"])

    def inventory(self) -> Decimal:
        for ap in self.info.user_state(self.addr, dex=self.market.hl_dex)["assetPositions"]:
            if ap["position"]["coin"] == self.market.hl_coin:
                return Decimal(ap["position"]["szi"])
        return Decimal(0)

    # -- quoting --------------------------------------------------------------
    def _px(self, px: Decimal) -> float:
        return round(float(f"{float(px):.5g}"), 6 - self.sz_dec)

    def quotes(self, center: Decimal, inventory: Decimal = Decimal(0),
               best_bid: Decimal | None = None, best_ask: Decimal | None = None) -> list[dict]:
        cfg = self.cfg
        ratio = max(Decimal(-1), min(Decimal(1), inventory / cfg.max_inventory))
        skewed = center * (1 - ratio * cfg.skew_bps / 10_000)  # long -> quote lower, short -> quote higher
        sz = float(cfg.size.quantize(Decimal(1).scaleb(-self.sz_dec)))
        out = []
        for lvl in range(1, cfg.levels + 1):
            off = cfg.spread_bps * lvl / 10_000
            for is_buy, px in ((True, skewed * (1 - off)), (False, skewed * (1 + off))):
                if is_buy and inventory >= cfg.max_inventory:
                    continue  # already too long: do not add
                if not is_buy and inventory <= -cfg.max_inventory:
                    continue
                price = self._px(px)
                crosses = (is_buy and best_ask is not None and Decimal(str(price)) >= best_ask) or \
                          (not is_buy and best_bid is not None and Decimal(str(price)) <= best_bid)
                out.append({"coin": self.market.hl_coin, "is_buy": is_buy, "sz": sz, "limit_px": price,
                            "order_type": {"limit": {"tif": "Gtc" if crosses else "Alo"}}, "reduce_only": False})
        return out

    def open_orders(self) -> list[dict]:
        return [o for o in self.info.open_orders(self.addr, dex=self.market.hl_dex) if o["coin"] == self.market.hl_coin]

    def cancel_all(self) -> None:
        orders = self.open_orders()
        if orders:
            self.exchange.bulk_cancel([{"coin": o["coin"], "oid": o["oid"]} for o in orders])

    def tick(self) -> None:
        """Cancel and replace all quotes around the (skewed) center."""
        self.cancel_all()
        bid, ask = self.best_bid_ask()
        res = self.exchange.bulk_orders(self.quotes(self.center_price(), self.inventory(), bid, ask))
        if res.get("status") != "ok":
            log.warning("quote refresh failed: %s", res)
            return
        errs = [s["error"] for s in res["response"]["data"]["statuses"] if "error" in s]
        if errs:
            log.warning("%d quote(s) rejected: %s", len(errs), errs[0])

    # -- inventory flatten ----------------------------------------------------
    def flatten(self, attempts: int = 3) -> Decimal:
        """Reduce-only IOC to flat. Returns the residual signed inventory."""
        q = Decimal(1).scaleb(-self.sz_dec)
        for _ in range(attempts):
            inv = self.inventory()
            if abs(inv) < q:
                return Decimal(0)
            is_buy = inv < 0  # short -> buy back
            mark = self.mark()
            limit = mark * (Decimal("1.01") if is_buy else Decimal("0.99"))
            res = self.exchange.order(self.market.hl_coin, is_buy, float(abs(inv)), self._px(limit),
                                      {"limit": {"tif": "Ioc"}}, True)
            log.info("flatten %s %s @<=%s: %s", "buy" if is_buy else "sell", abs(inv), limit, res.get("status"))
            time.sleep(0.5)
        residual = self.inventory()
        if abs(residual) >= q:
            log.warning("could not fully flatten: residual %s", residual)
        return residual

    def flatten_on_start(self) -> Decimal:
        """Inventory left over from a previous run is flattened (reduce-only) BEFORE quoting normally, as soon as the book has
        anything to take it. Waits up to flatten_wait_s; if the book never allows it, quoting starts anyway (skewed against
        the inventory) and the residual is returned. Returns the signed inventory still held."""
        q = Decimal(1).scaleb(-self.sz_dec)
        inv = self.inventory()
        if abs(inv) < q:
            return Decimal(0)
        self.cancel_all()  # a previous run's resting quotes would otherwise trade against our own flatten order
        log.warning("leftover inventory %s: flattening it (reduce-only) before quoting; waiting up to %ss for the book", inv,
                    self.cfg.flatten_wait_s)
        deadline = time.time() + self.cfg.flatten_wait_s
        while not self._stop:
            inv = self.inventory()
            if abs(inv) < q:
                log.info("startup inventory flattened")
                return Decimal(0)
            is_buy = inv < 0  # short -> buy back
            limit = self.mark() * (Decimal("1.01") if is_buy else Decimal("0.99"))
            if self.book_offers(is_buy, limit) > 0:  # the book allows (some of) it: send a reduce-only IOC
                self.flatten(attempts=1)
                if abs(self.inventory()) < q:
                    continue  # flat: the top of the loop reports it, no need to wait another tick
            if time.time() >= deadline:
                break
            self._sleep(self.cfg.refresh_s)
        inv = self.inventory()
        if abs(inv) >= q:
            log.warning("the book does not allow flattening %s yet: quoting normally, skewed against the inventory", inv)
        return inv

    # -- lifecycle ------------------------------------------------------------
    def run(self, manage_pid: bool = True) -> None:
        """Quote until stopped. manage_pid=False when embedded in `sereel serve`: the pidfile would hold the SERVER's pid, and
        `sereel mm stop` would then kill the server instead of just the maker."""
        log.warning("\n%s", BANNER)
        log.info("account abstraction: %s%s", self.mode,
                 " (unified: collateral is shared, no dex transfers needed)" if is_unified(self.mode) else
                 " (default: margin must be moved to the dex explicitly)")
        if self.margin() <= 0 and not is_unified(self.mode):
            log.warning("MM account has no balance on dex '%s'; fund it before quotes will rest", self.market.hl_dex)
        log.info("quoting %s around %s: %d levels/side, %s bps spacing, %s per level, max inventory %s, skew %s bps, refresh %ss",
                 self.market.hl_coin, self.cfg.center, self.cfg.levels, self.cfg.spread_bps, self.cfg.size,
                 self.cfg.max_inventory, self.cfg.skew_bps, self.cfg.refresh_s)
        p = pidfile(self.cfg.market_id)
        if manage_pid:
            p.parent.mkdir(exist_ok=True)
            p.write_text(str(os.getpid()))
        try:
            self.flatten_on_start()
            while not self._stop:
                try:
                    self.tick()
                except Exception as e:  # keep quoting through transient errors
                    log.error("tick failed: %s", e)
                self._sleep(self.cfg.refresh_s)
        except KeyboardInterrupt:
            log.warning("interrupted")
        finally:
            if manage_pid:
                p.unlink(missing_ok=True)
            self.shutdown()

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self._stop and time.time() < end:
            time.sleep(min(0.25, max(0.0, end - time.time())))

    def shutdown(self) -> None:
        """Cancel quotes, then flatten inventory with a reduce-only IOC."""
        try:
            self.cancel_all()
            residual = self.flatten()
            log.info("shutdown complete, residual inventory %s", residual)
        except Exception as e:
            log.error("shutdown failed (check open orders / inventory manually): %s", e)

    def stop(self) -> None:
        self._stop = True
