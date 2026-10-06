import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlmodel import SQLModel

from app import models  # noqa: F401
from app.db import BASELINE, _config, init_db
from alembic import command
from alembic.script import ScriptDirectory


@pytest.fixture()
def eng(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/m.db")
    yield e
    e.dispose()


def tables(e):
    with e.connect() as c:
        return set(inspect(c).get_table_names())


def test_upgrade_head_creates_every_model_table(eng):
    init_db(eng)
    assert tables(eng) == set(SQLModel.metadata.tables) | {"alembic_version"}


def test_models_and_migrations_have_not_drifted(eng):
    """Fails when a model changes without a migration: run `alembic revision --autogenerate -m '<what>'`."""
    init_db(eng)
    with eng.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), SQLModel.metadata)
    assert diff == [], f"models differ from migrations: {diff}"


def test_there_is_a_single_head():
    assert len(ScriptDirectory.from_config(_config(None)).get_heads()) == 1


def test_init_db_is_idempotent_and_keeps_data(eng):
    init_db(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO usednonce (nonce, created_at) VALUES ('n', '2026-01-01 00:00:00')"))
    init_db(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM usednonce")).scalar() == 1


def test_downgrade_to_base_removes_everything(eng):
    init_db(eng)
    with eng.begin() as conn:
        command.downgrade(_config(conn), "base")
    assert tables(eng) <= {"alembic_version"}


def test_legacy_pre_alembic_database_is_stamped_not_recreated(eng):
    """A database from before Alembic (baseline-era tables, no alembic_version) must upgrade in place, keeping its rows.
    It is built the way it really was: the baseline schema, with the version table removed."""
    with eng.begin() as conn:
        command.upgrade(_config(conn), "baseline")
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(text("INSERT INTO usednonce (nonce, created_at) VALUES ('legacy', '2026-01-01 00:00:00')"))
    init_db(eng)  # stamps baseline, then upgrades through every later revision
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == ScriptDirectory.from_config(
            _config(None)).get_current_head()
        assert c.execute(text("SELECT nonce FROM usednonce")).scalar() == "legacy"
        assert "activation_attempts" in {col["name"] for col in inspect(c).get_columns("strategy")}  # later revisions applied


def test_0002_upgrades_a_baseline_database_that_already_has_strategies(eng):
    """The NOT NULL columns added after the baseline must not break rows that exist before the upgrade."""
    with eng.begin() as conn:
        command.upgrade(_config(conn), "baseline")
        conn.execute(text(
            "INSERT INTO strategy (id, template, status, fund_id, fund_name, market_id, market_symbol, hedge_ratio_bps, leverage,"
            " rebalance_band_bps, target_exposure_units, return_wallet_address, owner_user_id, org_id, intent_id,"
            " registered_sender_address, multisig, expected_amount_usd, expires_at, margin_usd, size, entry_px, realized_pnl_usd,"
            " fees_usd, funding_usd, funding_cursor_ms, hl_order_ids, created_at, updated_at)"
            " VALUES ('s1','delta_neutral_hedge','active','f','','XAU-HL','XAU',6000,3,500,1,'w','','','i1','sender',0,100,"
            " '2026-01-01 00:00:00',0,0,0,0,0,0,0,'[]','2026-01-01 00:00:00','2026-01-01 00:00:00')"))
    init_db(eng)
    with eng.connect() as c:
        row = c.execute(text("SELECT required_margin_usd, activation_attempts FROM strategy WHERE id='s1'")).one()
    assert (float(row[0]), row[1]) == (0.0, 0)
