"""`sereel agent ...`: the AI layer's operator tools."""
import json

import logging

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
            size_oz: str = typer.Option(None, "--size-oz", help="A proposed hedge size in oz: judged against testnet depth (setup question)"),
            api_url: str = typer.Option("http://localhost:8000", "--api-url", help="Running service, used only with --strategy"),
            raw: bool = typer.Option(True, "--raw/--no-raw", help="Print Jev's raw response")):
    """Print the market snapshot Jev is given, its hash, and the live probabilities (or the rules fallback, labelled)."""
    from app.ai import jev
    from app.ai.questions import QUESTION_SET_VERSION
    from app.ai.signals import get_signals
    from app.ai.state import build_snapshot

    logging.getLogger("httpx").setLevel(logging.WARNING)
    markets = load_markets()
    if market not in markets:
        _fail(f"unknown market '{market}'")
    from decimal import Decimal

    snap = build_snapshot(markets[market], _strategy(api_url, strategy) if strategy else None,
                          size_oz=Decimal(size_oz) if size_oz else None)
    text = snap.to_state_text()
    console.print(text, markup=False)  # "[price]"-style section headers are text, not Rich markup
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


# ---- calibrate ----------------------------------------------------------------------------------------------------------
# Three fixed states built from TODAY's real snapshot with edited numbers. Each question must point the right way in each one;
# the table prints every probability, and the command exits 1 if any points wrong. Live Jev only (about 6 calls, a fraction of a
# cent); deliberately not part of pytest.
SYNTH_STRATEGY = {"id": "calibration", "status": "active", "leverage": 3, "target_exposure_units": 0.2, "hedge_ratio_bps": 6000,
                  "target_hedge_size_units": 0.12, "hedge_gap_units": 0.0036, "hedge_gap_bps": 300, "rebalance_band_bps": 500,
                  "required_margin_usd": 165.0, "value_usd": 400.0,
                  "position": {"size_units": 0.1164, "margin_usd": 400.0, "maintenance_margin_usd": 90.0, "unrealized_pnl_usd": -1.0,
                               "funding_paid_usd": 0.0, "mark_price_usd": 4126.0, "liquidation_price_usd": 5400.0}}

CALM = [("volatility", "realized_vol_24h_annualized", "15.00%"), ("volatility", "realized_vol_7d_annualized", "16.00%"),
        ("volatility", "vol_ratio_24h_7d", "0.94"), ("volatility", "move_1h_sigmas", "0.40"), ("volatility", "change_1h_pct", "0.050"),
        ("venue", "mark_vs_pyth_bps", "0.2"), ("venue", "funding_rate_annualized", "8.00%"),
        ("calendar", "events_within_24h", "none"),
        ("sizing", "depth_to_size_ratio", "20.00"), ("sizing", "testnet_depth_same_side_within_0.5pct_oz", "1.0000"),
        ("strategy", "maintenance_ratio", "4.50"), ("strategy", "equity_to_required_ratio", "2.60"), ("strategy", "gap_pct", "3.00"),
        ("sizing", "size_notional_usd", "15.00")]
STRESSED = [("volatility", "realized_vol_24h_annualized", "45.00%"), ("volatility", "realized_vol_7d_annualized", "20.00%"),
            ("volatility", "vol_ratio_24h_7d", "2.25"), ("volatility", "move_1h_sigmas", "4.20"), ("volatility", "change_1h_pct", "-3.200"),
            ("venue", "mark_vs_pyth_bps", "3.0"), ("venue", "funding_rate_annualized", "-4.00%"),
            ("calendar", "events_within_24h", "CPI release (September 2026 data)"),
            ("sizing", "depth_to_size_ratio", "0.20"), ("sizing", "testnet_depth_same_side_within_0.5pct_oz", "0.0100"),
            ("strategy", "maintenance_ratio", "1.30"), ("strategy", "equity_to_required_ratio", "0.80"), ("strategy", "gap_pct", "9.00"),
            ("sizing", "size_notional_usd", "52.00")]
DIVERGENT = CALM[:5] + [("venue", "mark_vs_pyth_bps", "60.0")] + CALM[6:]

# question -> expected direction per scenario: ">" means the probability must exceed 0.5, "<" that it must be below
EXPECT = {
    "calm": {"volatility_elevated": "<", "funding_favors_shorts": ">", "abnormal_price_move": "<", "venue_price_divergence": "<",
             "high_impact_event_soon": "<", "liquidity_sufficient_for_size": ">", "needs_top_up_soon": "<", "should_rebalance": "<",
             "liquidity_sufficient": ">", "excess_margin_safe_to_return": ">"},
    "stressed": {"volatility_elevated": ">", "funding_favors_shorts": "<", "abnormal_price_move": ">", "venue_price_divergence": "<",
                 "high_impact_event_soon": ">", "liquidity_sufficient_for_size": "<", "needs_top_up_soon": ">", "should_rebalance": ">",
                 "liquidity_sufficient": "<", "excess_margin_safe_to_return": "<"},
    "divergent": {"venue_price_divergence": ">", "volatility_elevated": "<", "abnormal_price_move": "<", "needs_top_up_soon": "<",
                  "funding_favors_shorts": ">", "high_impact_event_soon": "<"},
}


def _edited(snap, edits):
    import copy

    out = copy.deepcopy(snap)
    for sec, key, val in edits:
        if sec in out.sections:
            out.sections[sec][key] = val
    if "venue" in out.sections and any(k == "mark_vs_pyth_bps" for _, k, _ in edits):
        out.sections["venue"]["hl_mark"] = out.sections["venue"]["hl_mark"]  # the bps field is what the question reads
    return out


@agent_app.command("calibrate")
def calibrate(market: str = typer.Option("XAU-HL", "--market")):
    """Ask Jev the questions against three fixed states (calm, stressed, divergent) built from today's real snapshot with edited
    numbers, print the answers, and exit 1 if any answer points the wrong way. Uses live Jev; not part of pytest."""
    from decimal import Decimal

    from rich.table import Table

    from app.ai import jev
    from app.ai.questions import QUESTION_SET_VERSION, question_names
    from app.ai.state import build_snapshot

    logging.getLogger("httpx").setLevel(logging.WARNING)
    markets = load_markets()
    if market not in markets:
        _fail(f"unknown market '{market}'")
    base_market = build_snapshot(markets[market], None, size_oz=Decimal("0.05"))  # setup + market questions (a proposed 0.05 oz hedge)
    base_strategy = build_snapshot(markets[market], SYNTH_STRATEGY)  # monitoring questions (a synthetic live strategy)
    results: dict[str, dict[str, float]] = {}
    tokens = 0
    for name, edits in (("calm", CALM), ("stressed", STRESSED), ("divergent", DIVERGENT)):
        results[name] = {}
        for snap in (_edited(base_market, edits), _edited(base_strategy, edits)):
            names = [n for n in question_names(snap) if n in EXPECT[name]]
            if not names:
                continue
            try:
                r = jev.ask(snap.to_state_text(), names)
            except jev.JevError as e:
                _fail(f"Jev failed on the {name} state: {e}")
            tokens += r.usage.get("input_tokens", 0) + r.usage.get("output_tokens", 0)
            results[name].update({n: float(p) for n, p in r.probabilities.items()})
    table = Table(title=f"calibration (question set {QUESTION_SET_VERSION}, today's real snapshot with edited numbers)")
    table.add_column("question", no_wrap=True)
    for sc in EXPECT:
        table.add_column(sc, justify="right")
    wrong = []
    for q in sorted({q for sc in EXPECT.values() for q in sc}):
        row = [q]
        for sc, exp in EXPECT.items():
            if q not in exp:
                row.append("[dim]-[/]")
                continue
            p = results[sc].get(q)
            ok = p is not None and ((p > 0.5) if exp[q] == ">" else (p < 0.5))
            if not ok:
                wrong.append((sc, q, p, exp[q]))
            row.append(f"{'[green]ok[/]' if ok else '[red]WRONG[/]'} {p:.2f} ({exp[q]}.5)" if p is not None else "[red]missing[/]")
        table.add_row(*row)
    console.print(table)
    console.print(f"tokens used: {tokens}")
    if wrong:
        for sc, q, p, e in wrong:
            console.print(f"[red]{sc}: {q} = {p} but must be {e} 0.5[/]")
        raise typer.Exit(1)
    console.print("[green]all answers point the right way[/]")


@agent_app.command("key")
def key():
    """Print the agent's public key (what an owner grants a delegation to), creating the key if it does not exist. Never overwrites an
    existing key and never prints the secret."""
    from app import solana_client as sol

    existed = sol.agent_kp() is not None
    kp = sol.load_keypair(settings.agent_keypair, create=True)
    console.print(f"agent key {'kept' if existed else 'created'}: {settings.resolve(settings.agent_keypair)}")
    console.print(f"agent public key: [bold]{kp.pubkey()}[/]")
    console.print("It signs rebalances only for strategies whose owner granted it a delegation. It holds no funds and needs no SOL.")
    console.print("[yellow]Back up keys/ with the others.[/]")


@agent_app.command("log")
def show_log(n: int = typer.Option(20, "-n", help="How many of the latest entries"),
             strategy: str = typer.Option(None, "--strategy", help="Only this strategy (id or its first characters)"),
             raw: bool = typer.Option(False, "--raw", help="Print the JSON lines as written"),
             follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing new entries as they arrive")):
    """What Jev (or the rules) answered on every agent cycle, and what was decided. Reads AGENT_LOG_FILE."""
    from app.ai import signal_log

    path = signal_log._path()
    if path is None:
        _fail("AGENT_LOG_FILE is empty: the agent log is off")

    from rich.markup import escape

    def show(e: dict):
        if raw:
            typer.echo(json.dumps(e))  # plain echo: no wrapping, so each line stays one valid JSON document
        elif "error" in e:
            console.print(f"{e['at']}  {e['strategy_id'][:8]}  [red]ERROR {escape(e['error'])}[/] {escape(e.get('message', ''))}", soft_wrap=True)
        else:
            jev = e.get("jev") or {}
            src = f"jev {jev.get('latency_ms')}ms" if e["source"] == "jev" else f"rules ({e.get('jev_error', 'no jev')})"
            act = " " + e["action"]["type"] if e.get("action") else ""
            console.print(f"{e['at']}  {e['strategy_id'][:8]}  [bold]{escape(e['decision'] + act)}[/]  \\[{escape(src)}]  {escape(e['reason'])}", soft_wrap=True)
            console.print("    " + escape("  ".join(f"{k}={v}" for k, v in e["probabilities"].items())), soft_wrap=True)

    entries = signal_log.tail(n, strategy)
    if not entries and not follow:
        console.print(f"no entries yet in {path} (the agent logs one line per strategy per cycle; AGENT_ENABLED must be true)")
    for e in entries:
        show(e)
    if follow:
        import time

        seen = len(path.read_text().splitlines()) if path.exists() else 0
        while True:
            time.sleep(2)
            lines = path.read_text().splitlines() if path.exists() else []
            for line in lines[seen:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if strategy is None or e.get("strategy_id", "").startswith(strategy):
                    show(e)
            seen = len(lines)
