import logging
import signal
import time
from decimal import Decimal

import typer
import yaml
from rich.console import Console
from rich.table import Table

from app.config import ROOT, load_markets, settings
from app.errors import ServiceError

from . import setup
from .payouts import payouts_app

app = typer.Typer(help="Sereel execution service CLI", no_args_is_help=True)
mm_app = typer.Typer(help="Testnet market maker", no_args_is_help=True)
app.add_typer(mm_app, name="mm")
app.add_typer(payouts_app, name="payouts")
console = Console()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
FAUCET = "https://faucet.solana.com"


def _fail(msg: str, code: int = 1):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(code)


# ---- init -------------------------------------------------------------------

@app.command()
def init(
    force: bool = typer.Option(False, "--force", help="Replace existing keys (old ones are moved aside, never deleted)"),
    no_wait: bool = typer.Option(False, "--no-wait", help="Print the funding address and exit instead of waiting"),
    min_sol: float = typer.Option(1.0, help="Devnet SOL the funding wallet must hold before continuing"),
    poll_s: int = typer.Option(5, help="Seconds between balance checks while waiting"),
):
    """Create keys, wait for you to fund the funding wallet, distribute SOL, create the mint, check Hyperliquid."""
    from app import solana_client as sol

    try:
        settings.assert_network_safe()
    except RuntimeError as e:
        _fail(str(e))

    status = setup.ensure_keys(force)
    for name, s in status.items():
        console.print(f"keys/{name}: {s}")
    if any(s == "replaced" for s in status.values()):
        console.print("[yellow]--force: previous keys were moved aside as keys/*.bak-<timestamp>, not deleted.[/]")

    funding = sol.funding_kp().pubkey()
    console.print(f"\n[bold]Funding wallet address:[/] {funding}")
    need = Decimal(str(min_sol))
    bal = sol.sol_balance(funding)
    if bal < need:
        console.print(f"Balance {bal} SOL, need {need}. Send devnet SOL to that address at [cyan]{FAUCET}[/] (choose Devnet).")
        if no_wait:
            console.print("Re-run `sereel init` once it is funded.")
            raise typer.Exit(0)
        console.print("Waiting for the funds to arrive (Ctrl-C to stop, then re-run `sereel init`)...")
        try:
            while bal < need:
                time.sleep(poll_s)
                bal = sol.sol_balance(funding)
        except KeyboardInterrupt:
            _fail("stopped while waiting; keys are saved, re-run `sereel init` after funding")
    console.print(f"Funding wallet holds {bal} SOL.")

    console.print("\nDistributing SOL from the funding wallet:")
    result = setup.distribute_sol(lambda m: console.print(f"  {m}"))

    t = Table("item", "value", title="sereel init")
    t.add_row("solana network", "devnet" if sol.is_devnet() else settings.solana_rpc_url)
    t.add_row("funding wallet", str(funding))
    if not settings.stablecoin_mint:
        if any(v.startswith("skipped") for k, v in result.items() if k == "mint_authority"):
            t.add_row("stablecoin mint", "[yellow]not created: mint authority has no SOL[/]")
        else:
            mint = sol.create_mint(sol.mint_authority_kp(), sol.mint_authority_kp())
            setup.set_env_value("STABLECOIN_MINT", str(mint))
            settings.stablecoin_mint = str(mint)
            t.add_row("stablecoin mint", f"{mint} (created, written to .env)")
    else:
        t.add_row("stablecoin mint", settings.stablecoin_mint)

    t.add_row("hyperliquid", settings.hl_api_url)
    if settings.venue == "simulated":
        t.add_row("venue", "simulated (Hyperliquid checks skipped)")
    elif not (settings.hl_account_address and settings.hl_api_wallet_key):
        t.add_row("hyperliquid creds", "[red]HL_ACCOUNT_ADDRESS / HL_API_WALLET_KEY missing[/]")
    else:
        try:
            from app.venue.hyperliquid import HyperliquidVenue

            v = HyperliquidVenue(load_markets())
            for mid, aid in v.asset_ids.items():
                m = v.markets[mid]
                t.add_row(f"asset id {m.hl_coin}", str(aid))
                t.add_row(f"mark / oracle {m.hl_coin}", f"{v.mark_price(mid)} / {v.oracle_price(mid)}")
                t.add_row(f"margin on dex '{m.hl_dex}'", str(v.margin_balance(mid)))
            t.add_row("master account mode", v.mode)
            t.add_row("master main perp withdrawable", v.info.user_state(v.master)["withdrawable"])
            t.add_row("master key (dev margin moves)", "set" if settings.hl_master_key else "not set")
        except ServiceError as e:
            t.add_row("hyperliquid", f"[red]{e.code}: {e.message}[/]")
    console.print(t)
    console.print("\n[bold yellow]Back up `keys/` and `.env` somewhere outside this repository now.[/] "
                  "They hold the only copies of your Solana and Hyperliquid keys; neither is recoverable.")


# ---- serve ------------------------------------------------------------------

@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000):
    """Start the API (the payout scheduler, deposit watcher and snapshots run inside it)."""
    import uvicorn

    try:
        settings.assert_network_safe()
    except RuntimeError as e:
        _fail(str(e))
    if not settings.api_key:
        _fail("API_KEY is empty: the API would reject every request. Set API_KEY in .env.")
    uvicorn.run("app.main:app", host=host, port=port)


# ---- market maker -----------------------------------------------------------

def _mm_profile(market: str):
    return ROOT / "profiles" / f"mm-{market}.yaml"


@mm_app.command("run")
def mm_run(
    market: str = typer.Option(..., "--market"),
    spread_bps: float = typer.Option(None, "--spread-bps", help="Distance of level 1 from the center, bps"),
    levels: int = typer.Option(None, "--levels"),
    size: float = typer.Option(None, "--size", help="Per-level size in coin units (0.02-0.05 by default)"),
    center: str = typer.Option(None, "--center", help="oracle | pyth | mark"),
    max_inventory: float = typer.Option(None, "--max-inventory"),
    save: bool = typer.Option(True, help="Save these settings as profiles/mm-<market>.yaml"),
):
    """Run the testnet market maker. Refuses unless HL_API_URL is testnet. Ctrl-C / `mm stop` cancels and flattens."""
    from app.mm.market_maker import MarketMaker, MMConfig, assert_testnet

    try:
        assert_testnet()
        markets = load_markets()
        if market not in markets:
            _fail(f"unknown market {market}; known: {', '.join(markets)}")
        prof = _mm_profile(market)
        saved = yaml.safe_load(prof.read_text()) if prof.exists() else {}

        def pick(flag, key, prompt, default):
            if flag is not None:
                return flag
            if saved.get(key) is not None:
                return saved[key]
            return typer.prompt(prompt, default=default)

        cfg = MMConfig(
            market_id=market,
            spread_bps=Decimal(str(pick(spread_bps, "spread_bps", "Spread per level (bps)", 10.0))),
            levels=int(pick(levels, "levels", "Levels per side", 3)),
            size=Decimal(str(pick(size, "size", "Size per level (coin units)", 0.03))),
            center=str(pick(center, "center", "Center on (oracle/pyth/mark)", "oracle")),
            max_inventory=Decimal(str(max_inventory)) if max_inventory is not None
            else (Decimal(str(saved["max_inventory"])) if saved.get("max_inventory") is not None else None),
        )
        if save:
            prof.parent.mkdir(exist_ok=True)
            prof.write_text(yaml.safe_dump({"spread_bps": float(cfg.spread_bps), "levels": cfg.levels, "size": float(cfg.size),
                                            "center": cfg.center, "max_inventory": float(cfg.max_inventory)}))
        mm = MarketMaker(markets[market], cfg)
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")
    signal.signal(signal.SIGTERM, lambda *_: mm.stop())  # `mm stop` -> graceful shutdown (cancel + reduce-only flatten)
    mm.run()


@mm_app.command("stop")
def mm_stop(market: str = typer.Option(..., "--market")):
    """Ask a running market maker to shut down: it cancels its quotes and flattens with a reduce-only IOC."""
    from app.mm.market_maker import request_stop

    if not request_stop(market):
        _fail(f"no running market maker found for {market}")
    console.print(f"stop requested for {market}; it is cancelling quotes and flattening inventory")


if __name__ == "__main__":
    app()
