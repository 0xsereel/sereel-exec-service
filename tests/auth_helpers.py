"""A signer that mirrors Cantina's client: builds the sereel-strategy-v1 message and signs it with ed25519."""
import time
import uuid

import base58
from nacl.signing import SigningKey

from app import auth


class Signer:
    def __init__(self, seed: bytes | None = None):
        self.key = SigningKey(seed) if seed else SigningKey.generate()
        self.pubkey = base58.b58encode(bytes(self.key.verify_key)).decode()

    def authorization(self, action: str, strategy_id: str, params: dict, *, ts: int | None = None, nonce: str | None = None,
                      network: str = "solana") -> dict:
        ts = int(time.time() * 1000) if ts is None else ts
        nonce = nonce or str(uuid.uuid4())
        message = auth.build_message(self.pubkey, action, strategy_id, params, nonce, ts, network)
        sig = base58.b58encode(self.key.sign(message.encode()).signature).decode()
        return {"message": message, "signature": sig, "nonce": nonce, "timestamp": ts, "publicKey": self.pubkey}
