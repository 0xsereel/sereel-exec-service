"""Live bug: a deposit was on-chain, finalized, with the right memo, and the watcher never saw it. It listed transactions by the
funding WALLET, but a plain SPL transfer to an existing token account references the token account, not its owner."""
import pytest

from app.state import state
from app.strategies import watcher
from test_strategies import M, SENDER, create, fund, get


def test_the_watcher_reads_the_token_account_not_the_wallet(api, fakechain):
    assert watcher.watch_address() == fakechain.ata != fakechain.funding
    fakechain.queried.clear()
    watcher.watch_once()
    assert fakechain.queried == {fakechain.ata}  # the wallet is never what it asks about


def test_a_plain_transfer_to_an_existing_token_account_is_credited(api, fakechain):
    """The exact live shape: wallet NOT among the transaction's accounts."""
    s = create(api)
    sig = fakechain.deposit(SENDER, 127.2, s["intent_id"])  # creates_ata=False: a plain transferChecked + memo
    assert fakechain.funding not in fakechain.touches[sig] and fakechain.ata in fakechain.touches[sig]
    assert watcher.watch_once()["transfers"] == 1
    assert get(api, s["id"])["status"] == "active" and state.venue.position(None, M).size != 0


def test_a_transfer_that_also_creates_the_token_account_is_credited_too(api, fakechain):
    s = create(api)
    sig = fakechain.deposit(SENDER, 127.2, s["intent_id"], creates_ata=True)  # the first deposit ever: references the wallet as well
    assert fakechain.funding in fakechain.touches[sig]
    assert watcher.watch_once()["transfers"] == 1 and get(api, s["id"])["status"] == "active"


def test_the_startup_baseline_comes_from_the_token_accounts_history(api, fakechain):
    from sqlmodel import Session

    from app.db import engine
    from app.models import WatcherCursor

    with Session(engine) as db:
        db.delete(db.get(WatcherCursor, "funding"))
        db.commit()
    old = fakechain.deposit(SENDER, 5)  # history that predates the service: on the token account only
    watcher.ensure_cursor()
    with Session(engine) as db:
        assert db.get(WatcherCursor, "funding").last_signature == old  # a wallet-based baseline would have found nothing


def test_a_deposit_made_while_the_service_was_down_is_found_after_restart(api, fakechain):
    """The live case: the transfer happened (08:09:42), the watcher had no way to see it, then the fix was deployed."""
    s = create(api)
    fakechain.deposit(SENDER, 127.2, s["intent_id"])  # lands; nothing is watching yet in this test's timeline
    assert get(api, s["id"])["status"] == "pending_funding"
    assert watcher.watch_once()["transfers"] == 1  # the next tick (or a restart) finds it
    assert get(api, s["id"])["status"] == "active"


def test_unrelated_wallet_activity_does_not_matter(api, fakechain):
    """SOL airdrops and the like touch only the wallet: not listed, not processed, and nothing breaks."""
    fakechain.n += 1
    fakechain.txs["airdrop"] = {"meta": {"err": None, "innerInstructions": [], "preTokenBalances": [], "postTokenBalances": []},
                                "transaction": {"message": {"instructions": []}}}
    fakechain.order.append("airdrop")
    fakechain.touches["airdrop"] = {fakechain.funding}  # references the wallet only
    assert watcher.watch_once()["transfers"] == 0


def test_the_funding_address_endpoint_still_reports_the_wallet_not_the_token_account(api, fakechain):
    """The frontend sends to the wallet (it derives the token account); the API must keep advertising the wallet."""
    assert api.get("/strategies/funding-address").json()["address"] == fakechain.funding
