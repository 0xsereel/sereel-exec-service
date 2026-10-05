"""Testnet-only two-sided quoter, run from a separate Hyperliquid account (HL_MM_*)."""
import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from ..config import Market, settings
from ..errors import ServiceError

log = logging.getLogger("sereel.mm")

BANNER = "=" * 72 + "\n  SEREEL MARKET MAKER: TESTNET LIQUIDITY ONLY. NOT FOR MAINNET.\n" + "=" * 72


@dataclass
class MMConfig:
    market_id: str
    spread_bps: Decimal = Decimal(10)  # distance of level 1 from the mark; level n sits at n * spread_bps
    levels: int = 3
    size: Decimal = Decimal("0.03")  # coin units per level, bounded by MM_MIN_SIZE..MM_MAX_SIZE
    refresh_s: float = 5

    def __post_init__(self):
        if not settings.mm_min_size <= self.size <= settings.mm_max_size:
            raise ServiceError("MM_SIZE_OUT_OF_RANGE",
                               f"size {self.size} outside {settings.mm_min_size}..{settings.mm_max_size} (MM_MIN_SIZE/MM_MAX_SIZE)")
        if self.levels < 1 or self.spread_bps <= 0:
            raise ServiceError("MM_BAD_CONFIG", "levels must be >= 1 and spread_bps > 0")


def assert_testnet() -> None:
    """Hard guard, independent of ALLOW_MAINNET."""
    if "testnet" not in settings.hl_api_url:
        raise ServiceError("MM_MAINNET_REFUSED", f"market maker refuses to run: HL_API_URL={settings.hl_api_url} is not testnet")


class MarketMaker:
    def __init__(self, market: Market, cfg: MMConfig):
        assert_testnet()
        if not (settings.hl_mm_account_address and settings.hl_mm_api_wallet_key):
            raise ServiceError("VENUE_NOT_CONFIGURED", "HL_MM_ACCOUNT_ADDRESS / HL_MM_API_WALLET_KEY not set", 503)
        self.market, self.cfg = market, cfg
        self.addr = settings.hl_mm_account_address
        dexs = ["", market.hl_dex] if market.hl_dex else None
        self.info = Info(settings.hl_api_url, skip_ws=True, perp_dexs=dexs)
        self.exchange = Exchange(Account.from_key(settings.hl_mm_api_wallet_key), settings.hl_api_url,
                                 account_address=self.addr, perp_dexs=dexs)
        meta = self.info.meta(dex=market.hl_dex)
        self.sz_dec = next(int(u["szDecimals"]) for u in meta["universe"] if u["name"] == market.hl_coin)
        self._stop = False

    def mark(self) -> Decimal:
        meta, ctxs = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": self.market.hl_dex})
        return next(Decimal(c["markPx"]) for u, c in zip(meta["universe"], ctxs) if u["name"] == self.market.hl_coin)

    def margin(self) -> Decimal:
        return Decimal(self.info.user_state(self.addr, dex=self.market.hl_dex)["marginSummary"]["accountValue"])

    def _px(self, px: Decimal) -> float:
        return round(float(f"{float(px):.5g}"), 6 - self.sz_dec)

    def quotes(self, mark: Decimal) -> list[dict]:
        sz = float(self.cfg.size.quantize(Decimal(1).scaleb(-self.sz_dec)))
        out = []
        for lvl in range(1, self.cfg.levels + 1):
            off = self.cfg.spread_bps * lvl / 10_000
            for is_buy, px in ((True, mark * (1 - off)), (False, mark * (1 + off))):
                out.append({"coin": self.market.hl_coin, "is_buy": is_buy, "sz": sz, "limit_px": self._px(px),
                            "order_type": {"limit": {"tif": "Alo"}}, "reduce_only": False})
        return out

    def open_orders(self) -> list[dict]:
        return [o for o in self.info.open_orders(self.addr, dex=self.market.hl_dex) if o["coin"] == self.market.hl_coin]

    def cancel_all(self) -> None:
        orders = self.open_orders()
        if orders:
            self.exchange.bulk_cancel([{"coin": o["coin"], "oid": o["oid"]} for o in orders])

    def tick(self) -> None:
        """Cancel and replace all quotes around the current mark."""
        self.cancel_all()
        res = self.exchange.bulk_orders(self.quotes(self.mark()))
        if res.get("status") != "ok":
            log.warning("quote refresh failed: %s", res)
            return
        errs = [s["error"] for s in res["response"]["data"]["statuses"] if "error" in s]
        if errs:
            log.warning("%d quote(s) rejected: %s", len(errs), errs[0])

    def run(self) -> None:
        log.warning("\n%s", BANNER)
        if self.margin() <= 0:
            log.warning("MM account has no balance on dex '%s'; fund it before quotes will rest", self.market.hl_dex)
        log.info("quoting %s: %d levels/side, %s bps spacing, %s per level, refresh %ss", self.market.hl_coin,
                 self.cfg.levels, self.cfg.spread_bps, self.cfg.size, self.cfg.refresh_s)
        try:
            while not self._stop:
                try:
                    self.tick()
                except Exception as e:  # keep quoting through transient errors
                    log.error("tick failed: %s", e)
                time.sleep(self.cfg.refresh_s)
        finally:
            try:
                self.cancel_all()
            except Exception as e:
                log.error("cancel on shutdown failed: %s", e)

    def stop(self) -> None:
        self._stop = True
