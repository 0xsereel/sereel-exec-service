"""`sereel payouts ...`: schedules live in the DB shared with `sereel serve`, which does the paying."""
import logging
import re
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.table import Table

from app import pyth
from app.config import ROOT
from app.db import init_db
from app.errors import ServiceError
from app.payments import scheduler, service
from app.util import iso

payouts_app = typer.Typer(help="Payouts: one-off and scheduled stablecoin payments", no_args_is_help=True)
console = Console()
INTERVALS = {"30s": 30, "1m": 60, "5m": 300, "1h": 3600, "1d": 86400, "1 day": 86400}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_FIXED_MEMO = "Revenue payment {seq}"
DEFAULT_PRICED_MEMO = "Revenue payment {seq} | {units} units @ {price}"


def _fail(msg: str):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(1)


def parse_interval(text: str) -> int:
    """30s | 1m | 5m | 1h | 1d | 1 day | custom seconds (a bare integer, or 90s / 2m / 3h / 2d)."""
    t = str(text).strip().lower()
    if t in INTERVALS:
        return INTERVALS[t]
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", t)
    if not m:
        raise typer.BadParameter(f"cannot read interval '{text}' (try 30s, 1m, 5m, 1h, 1d or a number of seconds)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_when(text: str) -> datetime:
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise typer.BadParameter(f"cannot read '{text}' as a date/time (use e.g. 2026-12-31T18:00:00Z)")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def profile_path(name: str) -> Path:
    if not NAME_RE.match(name):
        raise typer.BadParameter("profile name may only contain letters, digits, '.', '_' and '-'")
    return ROOT / "profiles" / f"{name}.yaml"


def _pick_feed() -> str:
    q = typer.prompt("Search Pyth for an asset (e.g. XAU, BTC, AAPL)")
    results = pyth.search_feeds(q)[:10]
    if not results:
        _fail(f"no Pyth feeds match '{q}'")
    for i, r in enumerate(results, 1):
        console.print(f"  {i}. {r['symbol']}  [dim]{r['id'][:14]}...[/]")
    n = typer.prompt("Pick one", type=int, default=1)
    if not 1 <= n <= len(results):
        _fail("not a valid choice")
    return results[n - 1]["id"]


def _create(profile: dict) -> None:
    """Start a profile as a live schedule (idempotent by name)."""
    try:
        existing = next((s for s in service.list_schedules() if s.name == profile["name"] and s.status in ("active", "paused")), None)
        if existing:
            console.print(f"already registered: '{profile['name']}' is {existing.status} (id {existing.id}); nothing to do")
            return
        body = service.ScheduleIn(**{k: v for k, v in profile.items() if k != "fund_id" or v},
                                  start_immediately=False)
        sc = service.create_schedule(body)
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")
    console.print(f"[green]started[/] '{sc.name}' every {sc.interval_seconds}s, first payment at {iso(sc.next_run)}. "
                  "It is paid by `sereel serve` (or `sereel payouts run <profile> --foreground`).")


@payouts_app.command("new")
def new(
    name: str = typer.Option(None, "--name"),
    fund_id: str = typer.Option(None, "--fund-id"),
    to: str = typer.Option(None, "--to", help="Revenue wallet address"),
    interval: str = typer.Option(None, "--interval", help="30s | 1m | 5m | 1h | 1d | seconds"),
    mode: str = typer.Option(None, "--mode", help="fixed | priced"),
    amount: float = typer.Option(None, "--amount", help="USD per payment (fixed mode)"),
    feed_id: str = typer.Option(None, "--feed-id", help="Pyth feed id (priced mode)"),
    units: float = typer.Option(None, "--units", help="Units of the asset per payment (priced mode)"),
    jitter: float = typer.Option(None, "--jitter", help="+/- % random variation on units (priced mode)"),
    memo: str = typer.Option(None, "--memo", help="Memo template: {seq} {units} {price} {amount}"),
    max_payments: int = typer.Option(None, "--max-payments"),
    end_at: str = typer.Option(None, "--end-at", help="ISO date/time to stop at"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation (and start it if --start)"),
    start: bool = typer.Option(None, "--start/--no-start", help="Start it now"),
):
    """Create a payout profile (interactive; every prompt has a flag) and optionally start it."""
    from app import solana_client as sol

    try:
        name = name or typer.prompt("1. Profile name")
        path = profile_path(name)
        fund_id = fund_id if fund_id is not None else typer.prompt("2. Fund ID")
        to = to or typer.prompt("3. Revenue wallet address")
        while not sol.is_valid_address(to):
            if yes:
                _fail(f"'{to}' is not a valid Solana address")
            console.print("[red]not a valid Solana address[/]")
            to = typer.prompt("3. Revenue wallet address")
        if interval is None:
            console.print("4. How often should payouts be made?  30s / 1m / 5m / 1h / 1 day / custom seconds")
            interval = typer.prompt("   Interval", default="1m")
        secs = parse_interval(interval)
        mode = mode or typer.prompt("5. Amount mode (fixed/priced)", default="fixed")
        if mode not in ("fixed", "priced"):
            _fail("mode must be 'fixed' or 'priced'")
        profile = {"name": name, "fund_id": fund_id, "to": to, "interval_seconds": secs, "amount_mode": mode}
        if mode == "fixed":
            profile["amount_usd"] = amount if amount is not None else typer.prompt("   Amount per payment (USD)", type=float)
        else:
            feed = feed_id or _pick_feed()
            profile["pricing"] = {
                "pyth_feed_id": feed,
                "units_per_payment": units if units is not None else typer.prompt("   Units per payment", type=float),
                "units_jitter_pct": jitter if jitter is not None else (5.0 if yes else typer.prompt("   Jitter % on units", type=float, default=5.0)),
            }
        default_memo = DEFAULT_PRICED_MEMO if mode == "priced" else DEFAULT_FIXED_MEMO
        profile["memo_template"] = memo or (default_memo if yes else typer.prompt("6. Memo template", default=default_memo))
        if max_payments is None and end_at is None and not yes:
            stop = typer.prompt("7. Stop condition (never / count / date)", default="never")
            if stop == "count":
                max_payments = typer.prompt("   After how many payments", type=int)
            elif stop == "date":
                end_at = typer.prompt("   Stop at (ISO date/time)")
        if max_payments is not None:
            profile["max_payments"] = max_payments
        if end_at is not None:
            profile["end_at"] = parse_when(end_at).isoformat()
        service.ScheduleIn(**profile)  # validate shape
        service.check_template(profile["memo_template"])
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")

    console.print("\n8. Summary")
    t = Table("setting", "value")
    for k, v in profile.items():
        t.add_row(k, str(v))
    t.add_row("saved to", str(path.relative_to(ROOT)))
    console.print(t)
    if not yes and not typer.confirm("Save this profile?", default=True):
        _fail("cancelled; nothing saved")
    path.parent.mkdir(exist_ok=True)
    if path.exists() and not yes and not typer.confirm(f"{path.name} exists. Overwrite?", default=False):
        _fail("cancelled; nothing saved")
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    console.print(f"saved {path.relative_to(ROOT)}")
    if start is None:
        start = False if yes else typer.confirm("9. Start now?", default=True)
    if start:
        init_db()
        _create(profile)


@payouts_app.command("run")
def run(profile: str = typer.Argument(..., help="Profile name (profiles/<name>.yaml) or path"),
        foreground: bool = typer.Option(False, "--foreground", help="Also run the payer in this process until Ctrl-C")):
    """Start a saved profile with no prompts."""
    path = Path(profile) if profile.endswith(".yaml") else profile_path(profile)
    if not path.exists():
        _fail(f"no profile at {path}")
    init_db()
    _create(yaml.safe_load(path.read_text()))
    if foreground:
        console.print("paying in the foreground; Ctrl-C (or SIGTERM) to stop")
        signal.signal(signal.SIGTERM, signal.default_int_handler)  # SIGTERM stops it like Ctrl-C
        sched = scheduler.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sched.shutdown(wait=False)


@payouts_app.command("list")
def list_():
    """Schedules with next run and totals."""
    init_db()
    t = Table("id", "name", "to", "every", "mode", "status", "paid", "total USD", "next run")
    for s in service.list_schedules():
        t.add_row(s.id[:8], s.name, f"{s.to[:6]}...{s.to[-4:]}", f"{s.interval_seconds}s", s.amount_mode, s.status,
                  f"{s.seq}" + (f"/{s.max_payments}" if s.max_payments else ""), f"{s.total_paid_usd:.2f}", iso(s.next_run) or "-")
    console.print(t)


def _status(ref: str, status: str, word: str):
    init_db()
    try:
        sc = service.set_status(ref, status)
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")
    console.print(f"{word} '{sc.name}' ({sc.id[:8]})")


@payouts_app.command("pause")
def pause(ref: str = typer.Argument(..., help="Schedule id or name")):
    _status(ref, "paused", "paused")


@payouts_app.command("resume")
def resume(ref: str = typer.Argument(..., help="Schedule id or name")):
    _status(ref, "active", "resumed")


@payouts_app.command("stop")
def stop(ref: str = typer.Argument(..., help="Schedule id or name")):
    _status(ref, "stopped", "stopped")


@payouts_app.command("send")
def send(to: str = typer.Option(..., "--to"), amount: float = typer.Option(..., "--amount"),
         memo: str = typer.Option("", "--memo")):
    """One-off payout."""
    init_db()
    try:
        p = service.send_payment(to, Decimal(str(amount)), memo)
    except ServiceError as e:
        _fail(f"{e.code}: {e.message}")
    console.print(f"[green]sent[/] {p.amount_usd:.6f} to {to}\n  signature: {p.signature}")
