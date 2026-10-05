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

    hl_api_url: str = "https://api.hyperliquid-testnet.xyz"
    hl_account_address: str = ""
    hl_api_wallet_key: str = ""
    hl_master_key: str = ""  # DEV ONLY: user-signed margin moves; API wallets cannot sign them
    hl_mm_account_address: str = ""
    hl_mm_api_wallet_key: str = ""

    venue: str = "hyperliquid"
    pyth_hermes_url: str = "https://hermes.pyth.network"
    pyth_api_key: str = ""
    pyth_mock_price: Decimal | None = None  # dev/tests only: fixed price for every feed
    max_leverage: int = 3
    margin_buffer_pct: Decimal = Decimal(20)
    rebalance_band_pct: Decimal = Decimal(5)
    max_price_deviation_bps: Decimal = Decimal(200)
    ioc_max_retries: int = 3
    intent_ttl_seconds: int = 3600
    intent_ttl_multisig_seconds: int = 7 * 24 * 3600

    allow_mainnet: bool = False
    api_key: str = ""
    cors_origins: str = ""
    database_url: str = "sqlite:///./service.db"

    @field_validator("pyth_mock_price", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

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


def load_markets(path: Path | None = None) -> dict[str, Market]:
    data = yaml.safe_load((path or ROOT / "markets.yaml").read_text()) or []
    return {m["market_id"]: Market(**m) for m in data}


settings = Settings()
