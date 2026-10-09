from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    solana_rpc_url: str = "https://api.devnet.solana.com"
    stablecoin_mint: str = ""
    mint_authority_keypair: str = "./keys/mint_authority.json"
    funding_keypair: str = "./keys/funding.json"
    attest_keypair: str = "./keys/attest.json"
    payment_source_keypair: str = "./keys/payment_source.json"
    agent_keypair: str = "./keys/agent.json"  # signs the agent's rebalances as a delegate; holds no funds and needs no SOL

    hl_connect_timeout_s: float = 60  # while connecting only: the one-off metadata downloads are large and Hyperliquid can be slow
    hl_connect_attempts: int = 4  # a failed connect step is retried (with a growing pause) before giving up
    hl_request_timeout_s: float = 15  # every Hyperliquid call: the SDK default is NO timeout, so a silent peer blocks a thread forever
    hl_api_url: str = "https://api.hyperliquid-testnet.xyz"
    hl_account_address: str = ""
    hl_api_wallet_key: str = ""
    hl_master_key: str = ""  # DEV ONLY: user-signed margin moves; API wallets cannot sign them
    hl_mm_account_address: str = ""
    hl_mm_api_wallet_key: str = ""

    venue: str = "hyperliquid"
    pyth_hermes_url: str = "https://pyth.dourolabs.app/hermes"
    pyth_api_key: str = ""
    pyth_mock_price: Decimal | None = None  # dev/tests only: fixed price for every feed
    max_leverage: int = 3
    margin_buffer_pct: Decimal = Decimal(20)
    min_order_usd: Decimal = Decimal(10)  # Hyperliquid rejects any order under $10 notional
    rebalance_band_pct: Decimal = Decimal(5)
    max_price_deviation_bps: Decimal = Decimal(200)
    ioc_max_retries: int = 3
    activation_grace_s: int = 120  # how long after funding completes a deploy that cannot proceed is retried before it fails and refunds
    rebalance_tolerance_pct: Decimal = Decimal(2)  # create: expected_amount_usd may be this far below the computed requirement
    mm_min_size: Decimal = Decimal("0.02")  # market maker per-level order size bounds (coin units)
    mm_max_size: Decimal = Decimal("0.05")
    intent_ttl_seconds: int = 3600
    intent_ttl_multisig_seconds: int = 7 * 24 * 3600

    # --- AI agent (all optional; the service runs without any of it) ---
    agent_enabled: bool = False
    agent_interval_s: int = 60
    agent_act_threshold: Decimal = Decimal("0.80")
    agent_suggest_threshold: Decimal = Decimal("0.50")
    agent_run_once_cooldown_s: int = 30  # a second run_once on the same strategy inside this returns the latest decision
    agent_exec_cap_s: int = 600  # at most one executed agent action per strategy in this window
    agent_exec_max_divergence_bps: Decimal = Decimal(50)  # execute is downgraded to propose when the execution venue is further from Pyth
    # --- x402 paid data feed (devnet; settles straight to the customer's wallet through a public facilitator) ---
    x402_facilitator_url: str = "https://x402.org/facilitator"
    x402_network: str = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"  # Solana devnet, CAIP-2
    x402_usdc_mint: str = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"  # Circle's devnet USDC: x402 only, NOT the service's own mock mint
    x402_attestation_delay_s: int = 3600  # attestations of executed actions are sold only this long after they happen
    x402_rate_per_min: int = 30  # per payer and per IP on the public route
    x402_timeout_s: float = 10
    x402_max_timeout_s: int = 60  # how long a signed payment stays valid (maxTimeoutSeconds)
    public_url: str = ""  # the externally reachable base URL (e.g. the ngrok domain); empty = taken from the request
    chat_session_ttl_h: int = 24
    chat_max_messages: int = 30  # user messages per session
    chat_max_chars: int = 4000  # per message
    chat_rate_per_min: int = 12  # per owner
    signals_source_network: str = "mainnet"  # read-only market signals; execution stays on HL_API_URL (testnet)
    signals_hl_url: str = "https://api.hyperliquid.xyz"  # info reads ONLY: never an Exchange, never a key
    jev_base_url: str = "https://gateway.ngrok.ai/v1"
    jev_api_key: str = ""
    jev_model: str = "jev-latest"
    jev_auth_header: str = "auto"  # auto | bearer | x-api-key: auto tries Authorization: Bearer first, then x-api-key on a 401
    jev_timeout_s: float = 10
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_max_tokens: int = 800
    llm_timeout_s: float = 20

    allow_mainnet: bool = False
    dev_auth_bypass: bool = False  # DEV ONLY: skip signed-message authorization. Refused when ALLOW_MAINNET=true.
    auth_max_age_s: int = 60  # a signed message older than this is rejected
    auth_future_skew_s: int = 30  # ... and one dated further ahead than this
    squads_cache_s: int = 60  # how long a multisig's member list is cached
    squads_program_id: str = "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf"
    migrate_on_start: bool = True  # run `alembic upgrade head` when the API starts; set false in production
    api_key: str = ""
    cors_origins: str = ""
    database_url: str = "sqlite:///./service.db"

    @field_validator("pyth_mock_price", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def signals_url(self) -> str:
        """Where market signals are read: mainnet info by default (testnet gold has thin books and 0% funding), or the
        execution testnet when SIGNALS_SOURCE_NETWORK=testnet."""
        return self.hl_api_url if self.signals_source_network == "testnet" else self.signals_hl_url

    @property
    def is_hl_testnet(self) -> bool:
        return "testnet" in self.hl_api_url

    @property
    def is_solana_mainnet(self) -> bool:
        return "mainnet" in self.solana_rpc_url

    def assert_network_safe(self) -> None:
        """Refuse to start on mainnet unless ALLOW_MAINNET=true."""
        if self.allow_mainnet:
            return
        if self.is_solana_mainnet:
            raise RuntimeError("SOLANA_RPC_URL looks like mainnet; set ALLOW_MAINNET=true to override")
        if not self.is_hl_testnet:
            raise RuntimeError("HL_API_URL is not testnet; set ALLOW_MAINNET=true to override")

    @property
    def auth_bypass_active(self) -> bool:
        """The bypass only ever works off mainnet, even if the flag is set."""
        return self.dev_auth_bypass and not self.allow_mainnet

    def assert_auth_config_safe(self) -> None:
        if self.dev_auth_bypass and self.allow_mainnet:
            raise RuntimeError("DEV_AUTH_BYPASS=true is not allowed together with ALLOW_MAINNET=true")

    def resolve(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else ROOT / path


class Market(BaseModel):
    market_id: str
    symbol: str
    venue: str = "hyperliquid"
    hl_coin: str
    hl_dex: str = ""
    pyth_feed_id: str
    max_leverage: int = 3
    max_staleness_s: int = 30
    unit: str = "units"  # what one unit of the asset is called in messages shown to people (gold: "oz")
    enabled: bool = True  # false lists the market as "coming_soon" and refuses new strategies on it

    @property
    def status(self) -> str:
        """The frontend's StrategyMarket.status: exactly "active" or "coming_soon" (a strict string comparison on its side).
        It comes from configuration only, never from live prices: a slow Pyth or Hyperliquid must not grey a market out."""
        return "active" if self.enabled else "coming_soon"


def load_markets(path: Path | None = None) -> dict[str, Market]:
    data = yaml.safe_load((path or ROOT / "markets.yaml").read_text()) or []
    return {m["market_id"]: Market(**m) for m in data}


settings = Settings()
