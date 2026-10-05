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


def test_legacy_create_all_database_is_stamped_not_recreated(eng):
    """A database from before Alembic (tables, no alembic_version) must upgrade in place, keeping its rows."""
    SQLModel.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("INSERT INTO usednonce (nonce, created_at) VALUES ('legacy', '2026-01-01 00:00:00')"))
    init_db(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == BASELINE
        assert c.execute(text("SELECT nonce FROM usednonce")).scalar() == "legacy"
