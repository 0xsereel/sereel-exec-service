"""`sereel init` building blocks: key generation, SOL distribution, .env editing."""
import re
import time
from decimal import Decimal
from pathlib import Path

from app import solana_client as sol
from app.config import ROOT, settings

# name -> (settings attribute holding the path, SOL it should hold once distributed from the funding wallet)
KEYS = {
    "funding": "funding_keypair",
    "attest": "attest_keypair",
    "mint_authority": "mint_authority_keypair",
    "payment_source": "payment_source_keypair",
    "agent": "agent_keypair",  # signs delegated rebalances; holds nothing, so it is not in SOL_TARGETS
}
SOL_TARGETS = {"attest": Decimal("0.2"), "mint_authority": Decimal("0.2"), "payment_source": Decimal("0.2")}
FUNDING_RESERVE = Decimal("0.3")  # the funding wallet keeps this for refund/withdrawal fees


def ensure_keys(force: bool = False) -> dict[str, str]:
    """Create missing keypairs. Existing ones are never touched unless force, and then they are moved aside to
    `<name>.json.bak-<timestamp>` first (so a funded key is never lost). Returns {name: 'created'|'kept'|'replaced'}."""
    out = {}
    for name, attr in KEYS.items():
        path = settings.resolve(getattr(settings, attr))
        if path.exists() and not force:
            out[name] = "kept"
            continue
        if path.exists():
            path.rename(path.with_name(f"{path.name}.bak-{int(time.time())}"))
        sol.load_keypair(str(path), create=True)
        out[name] = "replaced" if force else "created"
    return out


def distribute_sol(log=lambda msg: None) -> dict[str, str]:
    """Top attest / mint_authority / payment_source up to their target SOL from the funding wallet.
    Idempotent: only shortfalls are sent. Returns {name: 'sent'|'ok'|'skipped: <reason>'}."""
    funding = sol.funding_kp()
    spendable = sol.sol_balance(funding.pubkey()) - FUNDING_RESERVE
    out = {}
    for name, target in SOL_TARGETS.items():
        kp = sol.load_keypair(getattr(settings, KEYS[name]))
        need = target - sol.sol_balance(kp.pubkey())
        if need <= 0:
            out[name] = "ok"
        elif need > spendable:
            out[name] = f"skipped: funding wallet has {max(spendable, Decimal(0)):.3f} SOL spendable, needs {need:.3f}"
        else:
            sol.transfer_sol(funding, kp.pubkey(), need)
            spendable -= need
            out[name] = "sent"
        log(f"{name}: {out[name]}")
    return out


def set_env_value(key: str, value: str, env: Path | None = None) -> None:
    """Set KEY=value in .env, keeping every other line and any trailing `# comment` on that line."""
    env = env or ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    pat = re.compile(rf"^{re.escape(key)}=[^#\n]*?(\s*#.*)?$")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            comment = m.group(1) or ""
            lines[i] = f"{key}={value}" + (f"  {comment.strip()}" if comment else "")
            break
    else:
        lines.append(f"{key}={value}")
    env.write_text("\n".join(lines) + "\n")
