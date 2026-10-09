import os
import tempfile

# A real file database, not :memory: -- an in-memory engine shares ONE connection across threads, which would make the
# concurrency tests meaningless. A file gives every session its own connection and real locking, like production.
_DB_DIR = tempfile.mkdtemp(prefix="sereel-test-")
os.environ["PYTH_MOCK_PRICE"] = "2650"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_DIR}/test.db"
os.environ["VENUE"] = "simulated"
os.environ.setdefault("API_KEY", "test-key")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Empty, fully migrated tables for every test (the in-memory engine is shared)."""
    from sqlalchemy import text
    from sqlmodel import SQLModel

    from app import models  # noqa: F401
    from app.db import engine, init_db

    SQLModel.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    init_db()
    yield


from app.strategies import watcher as _watcher  # noqa: E402

_REAL_ENSURE_CURSOR = _watcher.ensure_cursor


@pytest.fixture(autouse=True)
def hermetic_ai_settings(monkeypatch, tmp_path):
    """The suite must not depend on the developer's .env: the AI and data-feed switches and keys are reset to their shipped defaults, and any test
    that needs one sets it explicitly."""
    from app.config import settings

    for name, value in (("agent_enabled", False), ("custody_proof_mode", "off"), ("public_url", ""), ("jev_api_key", ""), ("llm_api_key", ""),
                        ("signals_source_network", "mainnet")):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "agent_log_file", str(tmp_path / "logs" / "agent_signals.jsonl"))  # tests never write into the repo's logs/
    monkeypatch.setattr(settings, "agent_log_state", False)


@pytest.fixture(autouse=True)
def agent_key_in_tmp(tmp_path, monkeypatch):
    """No test may touch the real keys/ directory: the agent key lives in a per-test temporary path (absent until a test creates it)."""
    from app.config import settings

    monkeypatch.setattr(settings, "agent_keypair", str(tmp_path / "agent-key" / "agent.json"))


@pytest.fixture(autouse=True)
def no_devnet_at_startup(monkeypatch):
    """The API's startup takes the deposit watcher's baseline from devnet. Tests must never touch devnet, so it is a no-op unless
    a test uses the fake chain (which supplies a fake RPC and switches the real baseline back on)."""
    monkeypatch.setattr(_watcher, "ensure_cursor", lambda: None)


class FakeChain:
    """An in-memory Solana: inbound transfers to the funding address, finalized signature listing, refunds, memos."""

    def __init__(self, monkeypatch, tmp_path):
        from solders.keypair import Keypair

        from app import solana_client as sol
        from app.config import settings

        self.sol, self.keys = sol, {}
        for name, attr in (("funding", "funding_keypair"), ("attest", "attest_keypair"),
                           ("mint_authority", "mint_authority_keypair"), ("payment_source", "payment_source_keypair")):
            path = tmp_path / f"{name}.json"
            monkeypatch.setattr(settings, attr, str(path))
            self.keys[name] = sol.load_keypair(str(path), create=True)
        self.mint = str(Keypair().pubkey())
        monkeypatch.setattr(settings, "stablecoin_mint", self.mint)
        self.funding = str(self.keys["funding"].pubkey())
        self.ata = str(sol.ata(self.funding))  # the funding wallet's token account for the stablecoin
        self.order, self.txs = [], {}
        self.touches: dict[str, set[str]] = {}  # signature -> the accounts that transaction references
        self.queried: set[str] = set()  # every address a signature listing was requested for
        self.refunds, self.memos = [], []
        self.accounts: dict[str, dict] = {}  # address -> getAccountInfo value (for Squads reads)
        self.rpc_calls: list[str] = []
        self.refund_fails = self.attest_fails = self.payout_fails = False
        self.payouts: list[tuple] = []
        self.fetch_errors: set[str] = set()
        self.n = 0
        monkeypatch.setattr(_watcher, "ensure_cursor", _REAL_ENSURE_CURSOR)  # the fake RPC below answers it
        monkeypatch.setattr(sol, "rpc", self._rpc)
        monkeypatch.setattr(sol, "finalized_signatures_since", self._since)
        monkeypatch.setattr(sol, "get_parsed_tx", self._get_tx)
        monkeypatch.setattr(sol, "transfer_from", self._refund)
        monkeypatch.setattr(sol, "post_memo", self._memo)
        monkeypatch.setattr(sol, "pay", self._pay)

    def deposit(self, sender: str, amount, memo: str | None = None, creates_ata: bool = False) -> str:
        """An inbound transfer. Like a real plain SPL transfer it references the funding TOKEN ACCOUNT but not the owner wallet;
        only a transfer that also creates the token account (creates_ata) references the wallet too."""
        self.n += 1
        sig = f"dep{self.n}"
        self.touches[sig] = {self.ata, sender, "sender-token-account", "mint", "token-program"} | ({self.funding} if creates_ata else set())
        bal = lambda rows: [{"mint": self.mint, "owner": o, "uiTokenAmount": {"uiAmountString": str(a)}} for o, a in rows]
        ixs = [{"program": "spl-token", "parsed": {}}] + ([{"program": "spl-memo", "parsed": memo}] if memo else [])
        self.txs[sig] = {"meta": {"err": None, "innerInstructions": [],
                                  "preTokenBalances": bal([(self.funding, 0), (sender, 10_000)]),
                                  "postTokenBalances": bal([(self.funding, amount), (sender, 10_000 - float(amount))])},
                         "transaction": {"message": {"instructions": ixs}}}
        self.order.append(sig)
        return sig

    def _rpc(self, method, params=None):
        self.rpc_calls.append(method)
        if method == "getAccountInfo":
            if self.accounts.get("__down__"):
                raise self.sol.SolanaError("rpc down")
            return {"value": self.accounts.get(params[0])}
        assert method == "getSignaturesForAddress", method
        limit = params[1].get("limit", 1000)
        return [{"signature": s, "err": None} for s in reversed(self._listed(params[0]))][:limit]

    def _listed(self, address):
        """What a real RPC lists for an address: only the transactions that reference it. (Signatures added without an explicit
        account list are treated as touching the token account.)"""
        self.queried.add(address)
        return [s for s in self.order if address in self.touches.get(s, {self.ata})]

    def _since(self, address, until, limit=200, max_pages=20):
        listed = self._listed(address)
        i = listed.index(until) + 1 if until in listed else 0
        return list(listed[i:])

    def _get_tx(self, sig, commitment="finalized"):
        if sig in self.fetch_errors:
            raise self.sol.SolanaError("rpc down")
        return self.txs.get(sig)

    def _refund(self, source, to, amount, memo=None):
        if self.refund_fails:
            raise self.sol.SolanaError("refund tx failed")
        self.refunds.append((to, amount, memo))
        return f"refund{len(self.refunds)}"

    def _pay(self, to, amount, memo=None, source=None):
        if self.payout_fails:
            raise self.sol.SolanaError("payout tx failed")
        self.payouts.append((to, amount, memo))
        return f"payout{len(self.payouts)}"

    def _memo(self, memo):
        if self.attest_fails:
            raise self.sol.SolanaError("memo tx failed")
        self.memos.append(memo)
        return f"att{len(self.memos)}"


@pytest.fixture()
def fakechain(monkeypatch, tmp_path):
    return FakeChain(monkeypatch, tmp_path)


@pytest.fixture()
def api(monkeypatch, fakechain):
    """The real app (simulated venue, migrated DB) with the background jobs switched off: tests drive the watcher."""
    from fastapi.testclient import TestClient

    from app import main
    from app.config import settings
    from app.payments import scheduler
    from app.strategies import watcher

    class NoSched:
        def shutdown(self, wait=False):
            pass

    monkeypatch.setattr(settings, "api_key", "test-key")
    monkeypatch.setattr(scheduler, "start", lambda: NoSched())
    monkeypatch.setattr(watcher, "register", lambda sched: None)
    with TestClient(main.app) as c:
        c.headers.update({"X-Sereel-Key": "test-key"})
        yield c
