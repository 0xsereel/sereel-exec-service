"""Solana memo attestations: a compact JSON memo whose hash commits to the full record kept in the database."""
import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal

from .. import solana_client as sol

log = logging.getLogger("sereel.attest")
NETWORK = "hyperliquid-testnet"
MAX_MEMO_BYTES = 500


def jsonable(o):
    if isinstance(o, Decimal):
        return format(o.normalize(), "f")
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    return o


def record_hash(record: dict) -> str:
    """sha256 of the canonical JSON (sorted keys, no whitespace) of the full record."""
    return hashlib.sha256(json.dumps(jsonable(record), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def memo_for(strategy_id: str, fund_id: str, action: str, record: dict, extra: dict | None = None) -> str:
    """`extra` adds short keys to the memo (an agent action: who signed, the state it saw, the probabilities that triggered it); the
    full detail lives in `record`, which `h` commits to."""
    memo = {"v": 1, "id": strategy_id, "fund": fund_id, "a": action, "net": NETWORK, "h": record_hash(record), **(extra or {})}
    text = json.dumps(memo, separators=(",", ":"))
    if len(text.encode()) > MAX_MEMO_BYTES:  # an unusually long fund id must not stop the attestation
        memo["fund"] = fund_id[:32]
        text = json.dumps(memo, separators=(",", ":"))
    return text


def attest(strategy_id: str, fund_id: str, action: str, record: dict, extra: dict | None = None) -> str | None:
    """Post the memo from the attest key. Returns the signature, or None if posting failed (trading never waits on it)."""
    try:
        return sol.post_memo(memo_for(strategy_id, fund_id, action, record, extra))
    except Exception as e:
        log.error("attestation of %s %s failed: %s", action, strategy_id, e)
        return None
