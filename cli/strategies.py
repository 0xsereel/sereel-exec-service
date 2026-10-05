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
