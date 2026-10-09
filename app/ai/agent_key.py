"""The agent's delegate signing key (`keys/agent.json`): it only SIGNS messages, holds no funds and needs no SOL. The message format is the
same sereel-strategy-v1 one every client uses, so the agent goes through exactly the same verification, nonce replay cache and
authorization log as an owner's request; the only difference is that the server finds its public key in a grant, not as the owner."""
import time
import uuid

import base58

from .. import auth
from .. import solana_client as sol


def pubkey() -> str | None:
    kp = sol.agent_kp()
    return None if kp is None else str(kp.pubkey())


def sign_authorization(action: str, strategy_id: str, params: dict) -> dict:
    kp = sol.agent_kp()
    if kp is None:
        raise FileNotFoundError("the agent key does not exist; run `sereel agent key` (or `sereel init`)")
    wallet, nonce, ts = str(kp.pubkey()), str(uuid.uuid4()), int(time.time() * 1000)
    message = auth.build_message(wallet, action, strategy_id, params, nonce, ts)
    signature = base58.b58encode(bytes(kp.sign_message(message.encode()))).decode()
    return {"message": message, "signature": signature, "nonce": nonce, "timestamp": ts, "publicKey": wallet}
