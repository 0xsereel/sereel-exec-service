"""Signed-message authorization for the strategy actions that move no funds on Solana (edit, rebalance, close,
return excess, change owner). Cantina v4 contract:

    message   = sereel-strategy-v1|<network>|<wallet_pubkey>|<action>|<strategy_id>|<params_hash>|<nonce>|<timestamp>
    params_hash = lowercase hex sha256 of the canonical JSON of the action's params (keys sorted, no whitespace,
                  amounts as decimal strings, integers for whole-number fields; see FIELD_TYPES)
    signature = raw ed25519 over the UTF-8 message, base58

The server rebuilds the message itself and never trusts the client's string. A strategy is managed by its bound owner:
`owner_pubkey`, or any current member of `owner_multisig` (a Squads v4 multisig account, read on-chain).
"""
import base64
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import timedelta
from typing import NamedTuple

import base58
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete

from . import solana_client as sol
from .config import settings
from .db import engine
from .errors import ServiceError
from .models import Strategy, UsedNonce, now

log = logging.getLogger("sereel.auth")
PREFIX, NETWORK = "sereel-strategy-v1", "solana"
ACTIONS = ("return_excess", "close_strategy", "rebalance", "edit_hedge_settings", "change_owner")
NONCE_TTL_S = 120  # anything older than the 60s age limit is already rejected; keep a margin


# ---- canonical params ------------------------------------------------------------------------------------------------
# The exact format clients must produce (see the README):
#   * UTF-8 JSON, object keys sorted (by code point) at every level, no whitespace anywhere
#   * every amount, price or quantity is a DECIMAL STRING ("520.5"); JSON numbers are not allowed for them
#   * whole-number fields (basis points) are JSON integers; no floats anywhere
#   * strings are escaped as JSON.stringify does (", \, control characters); non-ASCII is left as is
MAX_SAFE_INT = 2 ** 53 - 1
DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]{1,18})?$")  # no sign, no exponent, no leading zeros, <= 18 decimals

# what each signed field must be: "decimal" (a decimal string), "int" (a JSON integer), "str" (a string)
FIELD_TYPES = {
    "hedge_ratio_bps": "int",
    "target_exposure_units": "decimal",
    "amount_usd": "decimal",
    "destination_wallet_address": "str",
    "owner_pubkey": "str",
    "owner_multisig": "str",
}


def canonical_json(o) -> str:
    if o is None:
        return "null"
    if o is True:
        return "true"
    if o is False:
        return "false"
    if isinstance(o, int):
        if abs(o) > MAX_SAFE_INT:
            raise ValueError("integer outside the safe range")
        return str(o)
    if isinstance(o, str):
        return json.dumps(o, ensure_ascii=False)
    if isinstance(o, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in o) + "]"
    if isinstance(o, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical_json(o[k]) for k in sorted(o)) + "}"
    raise ValueError(f"{type(o).__name__} is not allowed in signed params (amounts and prices must be decimal strings)")


def validate_params(params: dict) -> None:
    """Reject anything that is not in the canonical params format. A JSON number where a decimal string belongs is
    AUTHORIZATION_INVALID: two clients could otherwise serialise the same number differently and sign different bytes."""
    for key, value in params.items():
        kind = FIELD_TYPES.get(key)
        if kind is None:
            raise _invalid(f"params.{key} is not a signable field")
        if isinstance(value, float) or (kind == "decimal" and isinstance(value, int) and not isinstance(value, bool)):
            raise _invalid(f'params.{key} must be a decimal string such as "520.5", not a JSON number')
        if kind == "decimal":
            if not isinstance(value, str) or not DECIMAL_RE.match(value):
                raise _invalid(f'params.{key} must be a decimal string such as "520.5" '
                               "(digits with an optional fractional part: no sign, exponent or leading zeros)")
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_SAFE_INT:
                raise _invalid(f"params.{key} must be a non-negative JSON integer")
        elif not isinstance(value, str):
            raise _invalid(f"params.{key} must be a string")


def params_hash(params: dict) -> str:
    return hashlib.sha256(canonical_json(params).encode()).hexdigest()


def build_message(wallet: str, action: str, strategy_id: str, params: dict, nonce: str, timestamp: int,
                  network: str = NETWORK) -> str:
    return "|".join([PREFIX, network, wallet, action, strategy_id, params_hash(params), nonce, str(timestamp)])


# ---- verification -----------------------------------------------------------------------------------------------------

class Verified(NamedTuple):
    signer: str
    nonce: str
    timestamp: int


def _invalid(msg: str) -> ServiceError:
    return ServiceError("AUTHORIZATION_INVALID", msg, 403)


def verify(authorization, action: str, strategy_id: str, params: dict, now_ms: int | None = None) -> Verified:
    """Check the signed message. Order matters: nothing is stored (the nonce) until the signature is proven good, so
    an attacker cannot burn somebody else's nonces."""
    if not authorization:
        raise ServiceError("AUTHORIZATION_REQUIRED", "a signed `authorization` is required for this action", 401)
    if not isinstance(authorization, dict):
        raise _invalid("authorization must be an object")
    try:
        wallet, nonce, ts = authorization["publicKey"], authorization["nonce"], authorization["timestamp"]
        signature, client_message = authorization["signature"], authorization["message"]
    except KeyError as e:
        raise _invalid(f"authorization is missing {e.args[0]}")
    if isinstance(ts, bool) or not isinstance(ts, int):
        raise _invalid("authorization.timestamp must be an integer of Unix milliseconds")
    if not all(isinstance(v, str) for v in (wallet, nonce, signature, client_message)):
        raise _invalid("authorization fields must be strings")
    try:
        uuid.UUID(nonce)
    except ValueError:
        raise _invalid("authorization.nonce must be a UUID")
    if action not in ACTIONS:
        raise _invalid(f"unknown action {action}")
    try:
        key = base58.b58decode(wallet)
        if len(key) != 32:
            raise ValueError
        sig = base58.b58decode(signature)
    except ValueError:
        raise _invalid("publicKey or signature is not valid base58")

    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if now_ms - ts > settings.auth_max_age_s * 1000:
        raise _invalid(f"authorization is older than {settings.auth_max_age_s}s")
    if ts - now_ms > settings.auth_future_skew_s * 1000:
        raise _invalid(f"authorization is dated more than {settings.auth_future_skew_s}s in the future")

    validate_params(params)
    rebuilt = build_message(wallet, action, strategy_id, params, nonce, ts)  # from OUR view of the request
    if client_message != rebuilt:
        raise _invalid("authorization.message does not match this request")
    try:
        VerifyKey(key).verify(rebuilt.encode(), sig)
    except (BadSignatureError, ValueError):
        raise _invalid("signature is not valid for this request")

    with Session(engine) as s:
        s.exec(delete(UsedNonce).where(UsedNonce.created_at < now() - timedelta(seconds=NONCE_TTL_S)))
        s.add(UsedNonce(nonce=nonce))
        try:
            s.commit()
        except IntegrityError:
            raise _invalid("this authorization was already used (replay)")
    return Verified(wallet, nonce, ts)


# ---- Squads multisig membership ---------------------------------------------------------------------------------------

SQUADS_DISCRIMINATOR = hashlib.sha256(b"account:Multisig").digest()[:8]
_squads_cache: dict[str, tuple[float, list[str]]] = {}


def parse_squads_multisig(raw: bytes) -> list[str]:
    """Members of a Squads v4 `Multisig` account: discriminator, create_key, config_authority, threshold u16,
    time_lock u32, transaction_index u64, stale_transaction_index u64, rent_collector Option<Pubkey>, bump u8,
    members Vec<{key, permissions mask u8}> (Borsh, little endian)."""
    if raw[:8] != SQUADS_DISCRIMINATOR:
        raise ValueError("not a Squads Multisig account")
    if len(raw) < 8 + 32 + 32 + 2 + 4 + 8 + 8 + 1 + 1 + 4:
        raise ValueError("account data is too short")
    o = 8 + 32 + 32 + 2 + 4 + 8 + 8
    o += 1 + (32 if raw[o] else 0)  # Option tag, then the key if Some
    o += 1  # bump
    n = int.from_bytes(raw[o:o + 4], "little")
    o += 4
    if n > 65_535 or o + n * 33 > len(raw):
        raise ValueError("member list does not fit the account data")
    return [base58.b58encode(raw[o + i * 33:o + i * 33 + 32]).decode() for i in range(n)]


def squads_members(address: str, fresh: bool = False) -> list[str]:
    """Current members of the multisig (cached briefly). ServiceError if it is not a Squads multisig account."""
    hit = _squads_cache.get(address)
    if hit and not fresh and time.time() - hit[0] < settings.squads_cache_s:
        return hit[1]
    try:
        res = sol.rpc("getAccountInfo", [address, {"encoding": "base64", "commitment": "confirmed"}])
    except Exception as e:
        raise ServiceError("CHAIN_UNAVAILABLE", f"could not read the multisig account: {e}", 503)
    value = res.get("value") if res else None
    if not value:
        raise ServiceError("BAD_REQUEST", f"{address} is not an account on this network", 400)
    if value["owner"] != settings.squads_program_id:
        raise ServiceError("BAD_REQUEST", f"{address} is not owned by the Squads program", 400)
    try:
        members = parse_squads_multisig(base64.b64decode(value["data"][0]))
    except (ValueError, IndexError):
        raise ServiceError("BAD_REQUEST", f"{address} is not a Squads multisig account", 400)
    _squads_cache[address] = (time.time(), members)
    return members


# ---- who may manage a strategy ---------------------------------------------------------------------------------------

def assert_signer_owns(st: Strategy, signer: str) -> None:
    if st.owner_pubkey:
        if signer != st.owner_pubkey:
            raise _invalid("signer is not the strategy's owner")
    elif st.owner_multisig:
        if signer not in squads_members(st.owner_multisig):
            raise _invalid("signer is not a current member of the strategy's owner multisig")
    else:
        raise _invalid("this strategy has no owner bound, so no signature can authorize it")


def authorize(authorization, action: str, st: Strategy, params: dict) -> str:
    """Return who acted: the verified signer, or 'dev-bypass' when the (non-mainnet) bypass is on."""
    if settings.auth_bypass_active:
        log.warning("!!! DEV_AUTH_BYPASS is ON: skipping signed-message authorization for %s on strategy %s !!!", action, st.id)
        return "dev-bypass"
    verified = verify(authorization, action, st.id, params)
    assert_signer_owns(st, verified.signer)
    return verified.signer
