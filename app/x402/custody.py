"""Custody proofs: "the custodian's cash balance is at least the reported fund NAV", as a yes/no claim for ONE published NAV checkpoint.

A proof NEVER carries a balance, an account number or any bank data: only whether the claim holds. It exists only for a checkpoint that
is already published (it is generated when the checkpoint is stored, never before, never ahead of the NAV), and `covers_nav_as_of` says
which one it backs.

Modes (CUSTODY_PROOF_MODE):
  off        no source: the field reads {"status": "unavailable", "reason": "custody proof source not configured"}.
  simulated  a deterministic DEMONSTRATION proof, labelled `mode: "simulated"` with a note, in every response. It is not a bank
             attestation, is never written into a Solana attestation memo, and is never described as verified anywhere.
  (verified  reserved: real zkTLS bank attestations. Implement `CustodyProofSource` and return it from `source()`; nothing else changes.)"""
import hashlib
import json
from typing import Protocol

from ..config import settings

PROVIDER = "zkTLS"
CUSTODIAN = "Standard Chartered (Straight2Bank)"
CLAIM = "custody cash balance >= reported fund NAV"
SIMULATED_NOTE = "Simulated proof for demonstration. Not a real bank attestation."
NOT_CONFIGURED = "custody proof source not configured"
PROOF_KEYS = ("mode", "provider", "custodian", "claim", "claim_holds", "covers_nav_as_of", "proof_id", "proof_hash", "generated_at")


class CustodyProofSource(Protocol):
    def proof_for(self, strategy_id: str, as_of: str, generated_at: str) -> dict:
        """The proof for the NAV checkpoint of `strategy_id` published `as_of` (ISO 8601 UTC), generated at `generated_at`."""


def canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class SimulatedSource:
    """Deterministic: the same strategy, as_of and generation time always give the same proof, and the claim always holds."""

    def proof_for(self, strategy_id: str, as_of: str, generated_at: str) -> dict:
        record = {"mode": "simulated", "provider": PROVIDER, "custodian": CUSTODIAN, "claim": CLAIM, "claim_holds": True, "covers_nav_as_of": as_of,
                  "proof_id": "sim-" + hashlib.sha256((strategy_id + as_of).encode()).hexdigest()[:12], "generated_at": generated_at,
                  "note": SIMULATED_NOTE}
        return {**record, "proof_hash": hashlib.sha256(canonical(record).encode()).hexdigest()}  # the hash covers everything but itself


def source() -> CustodyProofSource | None:
    """The configured source, or None when proofs are off (or the real source is not connected)."""
    return SimulatedSource() if settings.custody_proof_mode == "simulated" else None


def unavailable(reason: str = NOT_CONFIGURED) -> dict:
    return {"status": "unavailable", "reason": reason}


def generate(strategy_id: str, as_of: str, generated_at: str) -> dict | None:
    """Called once, when a NAV checkpoint is stored. None if there is no source (nothing is invented)."""
    src = source()
    return None if src is None else src.proof_for(strategy_id, as_of, generated_at)


def served(stored: dict | None) -> dict:
    """What a response carries for a checkpoint: its proof, or an unavailable object with the reason. With no source configured the
    answer is always "not configured", whatever was stored earlier."""
    if source() is None:
        return unavailable()
    if stored is None:
        return unavailable("no custody proof was generated for this NAV checkpoint")
    return dict(stored)
