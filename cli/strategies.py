"""`sereel strategies ...`: operator tools that work on the database on the host (not through the API)."""
import json
import os

import httpx
import typer
from rich.console import Console
from rich.table import Column, Table

from app.config import settings
from app.db import init_db
from app.errors import ServiceError

strategies_app = typer.Typer(help="Strategy operator tools", no_args_is_help=True)
console = Console()


def _fail(msg: str):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(1)


@strategies_app.command("set-owner")
def set_owner(strategy_id: str = typer.Argument(..., help="Strategy id"),
              pubkey: str = typer.Option(None, "--pubkey", help="The manager's Sereel Solana wallet"),
              multisig: str = typer.Option(None, "--multisig", help="A Squads v4 multisig ACCOUNT address (not the vault)")):
    """Bind an owner to a strategy that has none. Operator-only (no API route); refuses if an owner already exists,
    since changing an owner is the owner's own signed action. Attested with bound_by: operator."""
    from app import solana_client as sol
    from app.strategies import service

    if bool(pubkey) == bool(multisig):
        _fail("give exactly one of --pubkey or --multisig")
    init_db()
    try:
        st = service.operator_set_owner(strategy_id, pubkey, multisig)
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")
    console.print(f"[green]owner bound[/] on strategy {st.id}")
    console.print(f"  owner_pubkey:   {st.owner_pubkey}\n  owner_multisig: {st.owner_multisig}")
    if st.last_attestation_sig:
        console.print(f"  attestation (bound_by: operator): {sol.explorer_url(st.last_attestation_sig)}")
    else:
        console.print("[yellow]  the attestation could not be posted (check the attest key's SOL); the owner IS bound[/]")


@strategies_app.command("retry-withdrawal")
def retry_withdrawal(withdrawal_id: str = typer.Argument(..., help="Withdrawal id"),
                     confirm_not_sent: bool = typer.Option(False, "--confirm-not-sent",
                                                           help="You checked the chain: the failed payout was NOT sent")):
    """Operator-only: resume a FAILED withdrawal from the step it failed at. A payout whose outcome is unknown is retried only
    with --confirm-not-sent, after you have checked the funding wallet's transactions (memo: sereel <type> <id>)."""
    from app.strategies import withdrawals

    init_db()
    try:
        status = withdrawals.retry_withdrawal(withdrawal_id, confirm_not_sent)
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")
    w = withdrawals._load(withdrawal_id)
    console.print(f"withdrawal {withdrawal_id}: {w.status}" + (f" ({w.failure_reason})" if w.failure_reason else "") +
                  (f"\n  payout: {w.solana_signature}" if w.solana_signature else ""))
    if w.status == "failed":
        raise typer.Exit(1)


# ---- list: a client of the running API (it needs live marks, so it asks the service) ----------------------------------

TERMINAL = ("closed", "cancelled", "expired", "failed")
HEALTH_COLORS = ((7500, "green"), (4000, "yellow"), (0, "red"))


def _client(url: str) -> httpx.Client:
    return httpx.Client(base_url=url, headers={"X-Sereel-Key": settings.api_key}, timeout=60)


def fetch_strategies(url: str, owner: str | None = None, fund_id: str | None = None) -> list[dict]:
    params = {k: v for k, v in (("owner", owner), ("fund_id", fund_id)) if v}
    try:
        with _client(url) as c:
            r = c.get("/strategies", params=params)
    except httpx.ConnectError:
        _fail(f"cannot reach the service at {url}: is `sereel serve` running? (use --url to point elsewhere)")
    except httpx.TimeoutException:
        _fail(f"the service at {url} did not answer in time")
    if r.status_code == 401:
        _fail("the service rejected the API key: API_KEY in .env must match the one `sereel serve` was started with")
    if r.status_code != 200:
        _fail(f"{r.status_code}: {r.json().get('error', r.text) if r.headers.get('content-type', '').startswith('application/json') else r.text}")
    return r.json()


def _health_cell(bps: int | None) -> str:
    if bps is None:
        return "-"
    color = next(c for floor, c in HEALTH_COLORS if bps >= floor)
    return f"[{color}]{bps / 100:.1f}%[/]"


@strategies_app.command("list")
def list_(
    url: str = typer.Option(os.environ.get("SEREEL_URL", "http://localhost:8000"), "--url", help="Service base URL (env SEREEL_URL)"),
    all_: bool = typer.Option(False, "--all", help="Include closed, cancelled, expired and failed strategies"),
    owner: str = typer.Option(None, "--owner", help="Filter by owner_user_id"),
    fund_id: str = typer.Option(None, "--fund-id"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw API response"),
):
    """Strategies with their position, value and margin health (live, from the running service)."""
    rows = fetch_strategies(url, owner, fund_id)
    if not all_:
        rows = [r for r in rows if r["status"] not in TERMINAL]
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    t = Table(Column("id", no_wrap=True, min_width=8), Column("fund", no_wrap=True), "market", "status", "target", "size", "entry", "mark",
              "value USD", "unrealized", "health", Column("owner", no_wrap=True), title=f"strategies ({len(rows)})")
    for r in rows:
        p = r.get("position")
        value = (p["margin_usd"] + p["unrealized_pnl_usd"]) if p else None  # the v4 value_usd definition
        owner_ = r.get("owner_pubkey") or r.get("owner_multisig") or "-"
        t.add_row(r["id"][:8], r["fund_id"], r["market_symbol"], r["status"] + (" [dim](closed market)[/]" if r.get("market_closed") else ""),
                  f"{r['target_hedge_size_units']:g}",
                  f"{p['size_units']:g} {p['side']}" if p else "-", f"{p['entry_price_usd']:.2f}" if p else "-",
                  f"{p['mark_price_usd']:.2f}" if p else "-", f"{value:.2f}" if value is not None else "-",
                  f"{p['unrealized_pnl_usd']:+.2f}" if p else "-", _health_cell(p["margin_health_bps"]) if p else "-",
                  f"{owner_[:4]}...{owner_[-4:]}" if len(owner_) > 10 else owner_)
    console.print(t)
    pending = [r for r in rows if r["status"] == "pending_funding"]
    for r in pending:
        short = f", shortfall {r['shortfall_usd']}" if r.get("shortfall_usd") else ""
        console.print(f"  [yellow]{r['id'][:8]} pending_funding[/]: expected {r['expected_amount_usd']} USD, received "
                      f"{r['received_amount_usd'] or 0}{short}, expires {r['expires_at']}"
                      + (f"\n    {r['failure_reason']}" if r.get("failure_reason") else ""))
