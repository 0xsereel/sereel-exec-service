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
        self.order, self.txs = [], {}
        self.refunds, self.memos = [], []
        self.refund_fails = self.attest_fails = False
        self.fetch_errors: set[str] = set()
        self.n = 0
        monkeypatch.setattr(sol, "rpc", self._rpc)
        monkeypatch.setattr(sol, "finalized_signatures_since", self._since)
        monkeypatch.setattr(sol, "get_parsed_tx", self._get_tx)
        monkeypatch.setattr(sol, "transfer_from", self._refund)
        monkeypatch.setattr(sol, "post_memo", self._memo)

    def deposit(self, sender: str, amount, memo: str | None = None) -> str:
        self.n += 1
        sig = f"dep{self.n}"
        bal = lambda rows: [{"mint": self.mint, "owner": o, "uiTokenAmount": {"uiAmountString": str(a)}} for o, a in rows]
        ixs = [{"program": "spl-token", "parsed": {}}] + ([{"program": "spl-memo", "parsed": memo}] if memo else [])
        self.txs[sig] = {"meta": {"err": None, "innerInstructions": [],
                                  "preTokenBalances": bal([(self.funding, 0), (sender, 10_000)]),
                                  "postTokenBalances": bal([(self.funding, amount), (sender, 10_000 - float(amount))])},
                         "transaction": {"message": {"instructions": ixs}}}
        self.order.append(sig)
        return sig

    def _rpc(self, method, params=None):
        assert method == "getSignaturesForAddress", method
        limit = params[1].get("limit", 1000)
        return [{"signature": s, "err": None} for s in reversed(self.order)][:limit]

    def _since(self, address, until, limit=200, max_pages=20):
        i = self.order.index(until) + 1 if until in self.order else 0
        return list(self.order[i:])

    def _get_tx(self, sig, commitment="finalized"):
        if sig in self.fetch_errors:
            raise self.sol.SolanaError("rpc down")
        return self.txs.get(sig)

    def _refund(self, source, to, amount, memo=None):
        if self.refund_fails:
            raise self.sol.SolanaError("refund tx failed")
        self.refunds.append((to, amount, memo))
        return f"refund{len(self.refunds)}"

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
