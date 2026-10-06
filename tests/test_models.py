from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import models as m


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        yield s


def strat(**kw):
    base = dict(fund_id="f1", market_id="XAU-HL", hedge_ratio_bps=6000, leverage=3, rebalance_band_bps=500,
                target_exposure_units=Decimal("1000"), return_wallet_address="W", registered_sender_address="S",
                expected_amount_usd=Decimal("100"), expires_at=m.now() + timedelta(hours=1))
    base.update(kw)
    return m.Strategy(**base)


def test_strategy_roundtrip_defaults_and_decimal_precision(db):
    s = strat(target_exposure_units=Decimal("0.123456789012"))
    db.add(s); db.commit(); db.refresh(s)
    assert s.status == m.S_PENDING and s.template == "delta_neutral_hedge"
    assert s.received_amount_usd is None and s.size == 0 and s.intent_id and s.intent_id != s.id
    assert s.target_exposure_units == Decimal("0.123456789012")


def test_intent_ids_unique(db):
    db.add(strat(intent_id="x")); db.commit()
    db.add(strat(intent_id="x"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_withdrawal_nonce_is_idempotency_key(db):
    kw = dict(strategy_id="s", destination_wallet_address="D", auth_nonce="n1")
    db.add(m.Withdrawal(**kw)); db.commit()
    db.add(m.Withdrawal(**kw))
    with pytest.raises(IntegrityError):
        db.commit()


def test_chain_transfer_signature_cannot_repeat(db):
    db.add(m.ChainTransfer(signature="sig", disposition="credited")); db.commit()
    db.add(m.ChainTransfer(signature="sig", disposition="refunded"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_nonce_replay_rejected(db):
    db.add(m.UsedNonce(nonce="a")); db.commit()
    db.add(m.UsedNonce(nonce="a"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_snapshot_lookup_as_of(db):
    t0 = m.now()
    for i in range(3):
        db.add(m.PnlSnapshot(strategy_id="s", ts=t0 + timedelta(minutes=i), hedge_pnl_usd=Decimal(i)))
    db.commit()
    q = select(m.PnlSnapshot).where(m.PnlSnapshot.strategy_id == "s", m.PnlSnapshot.ts <= t0 + timedelta(seconds=90)) \
        .order_by(m.PnlSnapshot.ts.desc())
    assert db.exec(q).first().hedge_pnl_usd == 1
