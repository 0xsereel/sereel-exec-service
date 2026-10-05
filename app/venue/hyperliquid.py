"""Hyperliquid testnet venue with HIP-3 (builder dex) market resolution.

The service trades with an API (agent) wallet, which can place and cancel orders but cannot withdraw. Moving USDC
between the main perp balance and a builder dex is a user-signed action, so it needs the master key
(HL_MASTER_KEY, dev only; in production that signature comes from the manager's passkey flow).
"""
import logging
import time
from decimal import Decimal

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from ..config import Market, settings
from ..errors import ServiceError
from .base import PositionState, VenueAdapter

log = logging.getLogger("sereel.venue")

UNIFIED_MODES = ("unifiedAccount", "portfolioMargin")


def account_mode(info: Info, address: str) -> str:
    """Hyperliquid account abstraction: default | unifiedAccount | portfolioMargin | dexAbstraction | disabled."""
    r = info.post("/info", {"type": "userAbstraction", "user": address})
    return r if isinstance(r, str) else str(r)


def is_unified(mode: str) -> bool:
    """Unified/portfolio accounts share collateral across spot and every perp dex: no dex transfers exist or are needed."""
    return mode in UNIFIED_MODES


class HyperliquidVenue(VenueAdapter):
    name = "hyperliquid-testnet"

    def __init__(self, markets: dict[str, Market]):
        super().__init__(markets)
        if not (settings.hl_account_address and settings.hl_api_wallet_key):
            raise ServiceError("VENUE_NOT_CONFIGURED", "HL_ACCOUNT_ADDRESS / HL_API_WALLET_KEY not set", 503)
        self.master = settings.hl_account_address
        self.account_key = self.master.lower()
        dexs = [""] + sorted({m.hl_dex for m in markets.values() if m.venue == "hyperliquid" and m.hl_dex})
        self.info = Info(settings.hl_api_url, skip_ws=True, perp_dexs=dexs)
        self.exchange = Exchange(Account.from_key(settings.hl_api_wallet_key), settings.hl_api_url,
                                 account_address=self.master, perp_dexs=dexs)
        self._master_exchange = (Exchange(Account.from_key(settings.hl_master_key), settings.hl_api_url,
                                          account_address=self.master, perp_dexs=dexs)
                                 if settings.hl_master_key else None)
        self.asset_ids: dict[str, int] = {}
        self._sz_dec: dict[str, int] = {}
        self._max_lev: dict[str, int] = {}
        self.resolve_markets()
        self.mode = account_mode(self.info, self.master)
        log.info("master account abstraction: %s", self.mode)
        if is_unified(self.mode):
            log.warning("master is %s, expected 'default': dex margin transfers will be skipped and per-dex "
                        "reconciliation is not meaningful", self.mode)

    # -- market resolution ----------------------------------------------------
    def resolve_markets(self) -> None:
        """asset id = 100000 + dex_index * 10000 + index_in_meta, from perpDexs (never hardcoded), cross-checked
        against the SDK's own mapping."""
        dexs = self.info.perp_dexs()
        for mid, m in self.markets.items():
            if m.venue != "hyperliquid":
                continue
            if not m.hl_dex:
                dex_idx, meta = 0, self.info.meta()
            else:
                dex_idx = next((i for i, d in enumerate(dexs) if d and d["name"] == m.hl_dex), None)
                if dex_idx is None:
                    raise ServiceError("UNKNOWN_MARKET", f"dex {m.hl_dex} not found on {settings.hl_api_url}", 404)
                meta = self.info.meta(dex=m.hl_dex)
            idx = next((i for i, u in enumerate(meta["universe"]) if u["name"] == m.hl_coin), None)
            if idx is None:
                raise ServiceError("UNKNOWN_MARKET", f"{m.hl_coin} not in the {m.hl_dex or 'main'} universe", 404)
            asset = (100000 + dex_idx * 10000 + idx) if dex_idx else idx
            sdk_asset = self.info.name_to_asset(m.hl_coin)
            if sdk_asset != asset:
                raise ServiceError("UNKNOWN_MARKET", f"asset id mismatch for {m.hl_coin}: computed {asset}, SDK {sdk_asset}", 500)
            self.asset_ids[mid] = asset
            self._sz_dec[mid] = int(meta["universe"][idx]["szDecimals"])
            self._max_lev[mid] = int(meta["universe"][idx]["maxLeverage"])

    def size_decimals(self, market_id):
        self.market(market_id)
        return self._sz_dec[market_id]

    def maintenance_rate(self, market_id):
        """Hyperliquid maintenance margin is 1 / (2 * the asset's max leverage)."""
        self.market(market_id)
        return Decimal(1) / (2 * self._max_lev[market_id])

    # -- leverage -------------------------------------------------------------
    def leverage_status(self, market_id):
        m = self.market(market_id)
        d = self.info.post("/info", {"type": "activeAssetData", "user": self.master, "coin": m.hl_coin})
        lev = d.get("leverage") or {}
        return {"leverage": int(lev["value"]), "mode": lev["type"]} if lev else None

    def ensure_leverage(self, market_id) -> dict:
        """Set the market to its configured leverage: isolated if the dex accepts it, else cross. Idempotent, and
        verified by reading the setting back, so we never trade on a leverage we did not confirm."""
        m = self.market(market_id)
        want = m.max_leverage
        cur = self.leverage_status(market_id)
        if cur and cur["leverage"] == want:
            return cur
        for is_cross in (False, True):  # isolated first (HIP-3 dexes allow it), cross as the fallback
            res = self.exchange.update_leverage(want, m.hl_coin, is_cross=is_cross)
            if res.get("status") == "ok":
                break
            log.warning("update_leverage(%sx, %s) rejected: %s", want, "cross" if is_cross else "isolated", res)
        got = self.leverage_status(market_id)
        if not got or got["leverage"] != want:
            raise ServiceError("LEVERAGE_NOT_SET", f"could not set {m.hl_coin} to {want}x (venue reports {got}); no order sent", 503)
        log.info("%s leverage set: %sx %s", m.hl_coin, got["leverage"], got["mode"])
        return got

    def prepare_market(self, market_id):
        self.ensure_leverage(market_id)

    # -- reads ----------------------------------------------------------------
    def oracle_price(self, market_id) -> Decimal:
        m = self.market(market_id)
        meta, ctxs = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": m.hl_dex})
        return next(Decimal(c["oraclePx"]) for u, c in zip(meta["universe"], ctxs) if u["name"] == m.hl_coin)

    def mark_price(self, market_id):
        m = self.market(market_id)
        meta, ctxs = self.info.post("/info", {"type": "metaAndAssetCtxs", "dex": m.hl_dex})
        for u, c in zip(meta["universe"], ctxs):
            if u["name"] == m.hl_coin:
                return Decimal(c["markPx"])
        raise ServiceError("UNKNOWN_MARKET", f"{m.hl_coin} has no asset ctx", 404)

    def position(self, strategy_id, market_id):
        m = self.market(market_id)
        st = self.info.user_state(self.master, dex=m.hl_dex)
        out = PositionState(mark=self.mark_price(market_id),
                            account_value=Decimal(st["marginSummary"]["accountValue"]),
                            margin_used=Decimal(st["marginSummary"]["totalMarginUsed"]))
        for ap in st["assetPositions"]:
            p = ap["position"]
            if p["coin"] == m.hl_coin:
                out.size = Decimal(p["szi"])
                out.entry_px = Decimal(p["entryPx"] or 0)
                out.unrealized_pnl = Decimal(p["unrealizedPnl"])
                out.liquidation_px = Decimal(p["liquidationPx"]) if p.get("liquidationPx") else None
        return out

    def available_liquidity(self, market_id, is_buy, limit_px):
        m = self.market(market_id)
        bids, asks = self.info.post("/info", {"type": "l2Book", "coin": m.hl_coin})["levels"]
        if is_buy:
            return sum((Decimal(l["sz"]) for l in asks if Decimal(l["px"]) <= limit_px), Decimal(0))
        return sum((Decimal(l["sz"]) for l in bids if Decimal(l["px"]) >= limit_px), Decimal(0))

    def funding_entries(self, market_id, since_ms):
        coin = self.market(market_id).hl_coin
        return [(int(e["time"]), Decimal(e["delta"]["usdc"])) for e in self.info.user_funding_history(self.master, since_ms)
                if e["delta"].get("coin") == coin and int(e["time"]) > since_ms]

    # -- margin ---------------------------------------------------------------
    def _usdc_token(self) -> str:
        for t in self.info.spot_meta()["tokens"]:
            if t["name"] == "USDC":
                return f"USDC:{t['tokenId']}"
        raise ServiceError("INSUFFICIENT_MARGIN", "USDC token not found in spot meta", 500)

    def _transfer(self, src_dex: str, dst_dex: str, amount: Decimal) -> None:
        if not self._master_exchange:
            raise ServiceError("INSUFFICIENT_MARGIN", "moving margin needs the master key (HL_MASTER_KEY in dev)", 503)
        res = self._master_exchange.send_asset(self.master, src_dex, dst_dex, self._usdc_token(), float(amount))
        if res.get("status") != "ok":
            raise ServiceError("INSUFFICIENT_MARGIN", f"transfer {src_dex or 'main'} -> {dst_dex or 'main'} failed: {res}")

    def _spot_usdc(self) -> Decimal:
        for b in self.info.spot_user_state(self.master)["balances"]:
            if b["coin"] == "USDC":
                return Decimal(b["total"]) - Decimal(b.get("hold", "0"))
        return Decimal(0)

    def ensure_margin(self, market_id, usd_amount):
        """Top the builder-dex balance up to usd_amount, drawing on the main perp balance first, then spot USDC."""
        m = self.market(market_id)
        if not m.hl_dex:
            return
        if is_unified(self.mode):
            spot = self._spot_usdc()
            if spot < Decimal(usd_amount):
                raise ServiceError("INSUFFICIENT_MARGIN", f"unified account holds {spot} USDC, need {usd_amount}")
            return  # collateral is shared; nothing to move
        have = Decimal(self.info.user_state(self.master, dex=m.hl_dex)["marginSummary"]["accountValue"])
        need = Decimal(usd_amount) - have
        if need <= 0:
            return
        main = Decimal(self.info.user_state(self.master)["withdrawable"])
        spot = self._spot_usdc()
        if need > main + spot:
            raise ServiceError("INSUFFICIENT_MARGIN",
                               f"need {need} more on {m.hl_dex}; main perp withdrawable is {main}, spot USDC is {spot}")
        from_main = min(need, main)
        if from_main > 0:
            self._transfer("", m.hl_dex, from_main)
        if need - from_main > 0:
            self._transfer("spot", m.hl_dex, need - from_main)

    def release_margin(self, market_id, usd_amount):
        m = self.market(market_id)
        if m.hl_dex and not is_unified(self.mode):
            self._transfer(m.hl_dex, "", Decimal(usd_amount))

    def withdraw_to_arbitrum(self, amount_usd: Decimal, destination: str | None = None) -> dict:
        """Withdraw USDC from the main perp balance through the Hyperliquid bridge to Arbitrum (Sepolia on testnet).

        Signed by the MASTER key: API wallets cannot withdraw. Hyperliquid charges a flat fee (about $1) and the
        funds land on Arbitrum after validators sign, typically within minutes. Pass destination to override the
        default (the master's own address).
        """
        if not self._master_exchange:
            raise ServiceError("WITHDRAW_NOT_AUTHORIZED", "withdrawals need the master key (HL_MASTER_KEY in dev)", 503)
        amount = Decimal(amount_usd)
        main = Decimal(self.info.user_state(self.master)["withdrawable"])
        if amount > main:
            raise ServiceError("WITHDRAW_BELOW_MARGIN", f"withdrawable on the main perp balance is {main}, asked for {amount}")
        res = self._master_exchange.withdraw_from_bridge(float(amount), destination or self.master)
        if res.get("status") != "ok":
            raise ServiceError("WITHDRAW_FAILED", f"bridge withdrawal rejected: {res}")
        return res

    def withdrawals_since(self, since_ms: int) -> list[dict]:
        """Ledger entries of type 'withdraw' for the master (hash, nonce, amount, fee), for confirmation."""
        return [u for u in self.info.post("/info", {"type": "userNonFundingLedgerUpdates", "user": self.master,
                                                    "startTime": since_ms})
                if u["delta"].get("type") == "withdraw"]

    # -- orders ---------------------------------------------------------------
    @staticmethod
    def _round_px(px: Decimal, sz_decimals: int) -> float:
        """Perps: at most 5 significant figures and at most (6 - szDecimals) decimals."""
        return round(float(f"{float(px):.5g}"), 6 - sz_decimals)

    def _ioc(self, market_id, is_buy, size, limit_px, reduce_only=False):
        m = self.market(market_id)
        res = self.exchange.order(m.hl_coin, is_buy, float(size), self._round_px(limit_px, self._sz_dec[market_id]),
                                  {"limit": {"tif": "Ioc"}}, reduce_only)
        if res.get("status") != "ok":
            raise ServiceError("ORDER_NOT_FILLED", f"order rejected: {res}")
        st = res["response"]["data"]["statuses"][0]
        if "filled" in st:
            return Decimal(st["filled"]["avgPx"]), str(st["filled"]["oid"])
        if "resting" in st:  # an IOC should never rest; cancel defensively
            self.exchange.cancel(m.hl_coin, st["resting"]["oid"])
            return None, str(st["resting"]["oid"])
        err = str(st.get("error", st))
        if "could not immediately match" in err:
            return None, None  # nothing crossed: zero fill, the caller measures the position
        if "nsufficient" in err and "margin" in err:
            raise ServiceError("INSUFFICIENT_MARGIN", err)
        raise ServiceError("ORDER_NOT_FILLED", f"order error: {err}")

    def _fees_for(self, market_id, oids, fallback_notional):
        if not oids:
            return Decimal(0)
        time.sleep(0.5)  # fills index shortly after the order response
        mine = [f for f in self.info.user_fills(self.master) if str(f["oid"]) in oids]
        return sum((Decimal(f["fee"]) for f in mine), Decimal(0)) if mine else fallback_notional * Decimal("0.0009")  # observed xyz taker fee, live


def get_venue(markets: dict[str, Market]) -> VenueAdapter:
    from .simulated import SimulatedVenue

    return SimulatedVenue(markets) if settings.venue == "simulated" else HyperliquidVenue(markets)
