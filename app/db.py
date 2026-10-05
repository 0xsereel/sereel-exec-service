from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

from .config import ROOT, settings

BASELINE = "baseline"  # revision id of the first migration; databases made by the old create_all() are stamped with it


def _resolve(url: str) -> str:
    """sqlite:///./x.db is relative to the project root, not to whatever directory the command was run from
    (otherwise `sereel payouts ...` and `sereel serve` would see different databases)."""
    prefix = "sqlite:///./"
    return f"sqlite:///{ROOT}/{url[len(prefix):]}" if url.startswith(prefix) else url


def database_url() -> str:
    return _resolve(settings.database_url)


_IN_MEMORY = settings.database_url in ("sqlite://", "sqlite:///:memory:")
if settings.database_url.startswith("sqlite"):
    _kw = {"connect_args": {"check_same_thread": False}}
    if _IN_MEMORY:
        _kw["poolclass"] = StaticPool  # one shared connection, or each session would see its own empty database
else:
    _kw = {}
engine = create_engine(database_url(), **_kw)


def _config(connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = connection  # migrate on the connection we hand over (works for in-memory too)
    return cfg


def init_db(eng=None) -> None:
    """`alembic upgrade head`. Every schema change ships as a migration; nothing else creates or alters tables.

    A database created by the pre-Alembic create_all() has the tables but no alembic_version: it is stamped at the
    baseline first (its schema is the baseline's) and then upgraded normally."""
    with (eng or engine).begin() as conn:
        tables = set(inspect(conn).get_table_names())
        cfg = _config(conn)
        if "alembic_version" not in tables and "strategy" in tables:
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")


def check_db_at_head(eng=None) -> None:
    """For MIGRATE_ON_START=false: refuse to run against a database that is not at the latest migration."""
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(_config(None)).get_current_head()
    with (eng or engine).connect() as conn:
        current = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() \
            if "alembic_version" in inspect(conn).get_table_names() else None
    if current != head:
        raise RuntimeError(f"database is at revision {current!r} but the code expects {head!r}; "
                           "run `alembic upgrade head` (MIGRATE_ON_START=false)")


def session() -> Session:
    return Session(engine)
