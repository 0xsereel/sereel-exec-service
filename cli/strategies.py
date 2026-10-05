"""`sereel strategies ...`: operator tools that work on the database on the host (not through the API)."""
import typer
from rich.console import Console

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
