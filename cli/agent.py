"""`sereel agent ...`: the AI layer's operator tools."""
import json

import httpx
import typer
from rich.console import Console

from app.config import load_markets, settings

agent_app = typer.Typer(help="AI agent tools", no_args_is_help=True)
console = Console(highlight=False)


def _fail(msg: str):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(1)


def _strategy(api_url: str, sid: str) -> dict:
    h = {"X-Sereel-Key": settings.api_key, "ngrok-skip-browser-warning": "true"}
    try:
        r = httpx.get(f"{api_url.rstrip('/')}/strategies/{sid}", headers=h, timeout=15)
        r.raise_for_status()
        st = r.json()
        v = httpx.get(f"{api_url.rstrip('/')}/strategies/{sid}/value", headers=h, timeout=15)
        st["value_usd"] = v.json().get("value_usd") if v.status_code == 200 else None
        return st
    except httpx.HTTPError as e:
        _fail(f"could not read strategy {sid} from {api_url}: {e}. Is `sereel serve` running?")


@agent_app.command("signals")
def signals(strategy: str = typer.Option(None, "--strategy", help="Strategy id: adds its position and the monitoring questions"),
            market: str = typer.Option("XAU-HL", "--market"),
            api_url: str = typer.Option("http://localhost:8000", "--api-url", help="Running service, used only with --strategy"),
            raw: bool = typer.Option(True, "--raw/--no-raw", help="Print Jev's raw response")):
    """Print the market snapshot Jev is given, its hash, and the live probabilities (or the rules fallback, labelled)."""
    from app.ai import jev
    from app.ai.questions import QUESTION_SET_VERSION
    from app.ai.signals import get_signals
    from app.ai.state import build_snapshot

    markets = load_markets()
    if market not in markets:
        _fail(f"unknown market '{market}'")
    snap = build_snapshot(markets[market], _strategy(api_url, strategy) if strategy else None)
    text = snap.to_state_text()
    console.print(text)
    console.print(f"state_hash: {snap.state_hash}")
    console.print(f"signals_network: {snap.signals_network}   execution_network: {snap.execution_network}   degraded: {snap.degraded}")
    sig = get_signals(snap)
    console.print(f"\nsignals_source: [bold]{sig.source}[/]   question_set: {QUESTION_SET_VERSION}")
    for n in sig.names:
        console.print(f"  {n:34s} {sig.probabilities[n]:.2f}")
    if sig.jev_result:
        r = sig.jev_result
        console.print(f"\njev: model={r.model} latency={r.latency_ms}ms usage={r.usage} auth_header_that_worked=[bold]{r.auth_header}[/]")
        if raw:
            console.print("raw response:\n" + json.dumps(r.raw, indent=2))
    else:
        console.print(f"\n[yellow]Jev was not used: {sig.jev_error}[/]")
        console.print(f"  base: {settings.jev_base_url}   model: {settings.jev_model}   auth setting: {settings.jev_auth_header}")
