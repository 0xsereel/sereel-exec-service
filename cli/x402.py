"""`sereel x402 ...`: act as a third-party agent buying a strategy's data feed (a demo client)."""
import json
import time

import typer
from rich.console import Console

from app import solana_client as sol
from app.config import settings

x402_app = typer.Typer(help="x402 data feed demo client", no_args_is_help=True)
console = Console(highlight=False)


@x402_app.command("buy")
def buy(strategy_id: str = typer.Argument(..., help="The strategy whose feed to buy"),
        keypair: str = typer.Option(..., "--keypair", help="The buyer's keypair JSON (needs Circle devnet USDC; no SOL, the facilitator pays fees)"),
        url: str = typer.Option(None, "--url", help="Service base URL (default: PUBLIC_URL, else http://localhost:8000)"),
        repeat: int = typer.Option(1, "--repeat", min=1, help="Buy this many times"),
        interval: float = typer.Option(5.0, "--interval", help="Seconds between purchases")):
    """Request, receive the 402, pay, retry, and print the data and the settlement transaction. With --repeat the customer's income visibly
    accrues (watch GET /strategies/{id}/data-feed)."""
    from app.x402 import client

    payer = sol.load_keypair(keypair)
    base = (url or settings.public_url or "http://localhost:8000").rstrip("/")
    endpoint = f"{base}/x402/strategies/{strategy_id}"
    console.print(f"buyer {payer.pubkey()} -> {endpoint}")
    for i in range(repeat):
        try:
            res = client.buy(endpoint, payer)
        except client.BuyError as e:
            console.print(f"[red]purchase {i + 1} failed: {e}[/]")
            raise typer.Exit(1)
        tx = res["settlement"]["transaction"]
        console.print(f"[green]purchase {i + 1}/{repeat}[/] paid {int(res['price']) / 1e6:.6f} USDC; settled: {sol.explorer_url(tx)}")
        console.print(json.dumps(res["data"], indent=2))
        if i < repeat - 1:
            time.sleep(interval)
