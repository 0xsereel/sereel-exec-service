from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from .config import ROOT, settings


def _resolve(url: str) -> str:
    """sqlite:///./x.db is relative to the project root, not to whatever directory the command was run from
    (otherwise `sereel payouts ...` and `sereel serve` would see different databases)."""
    prefix = "sqlite:///./"
    return f"sqlite:///{ROOT}/{url[len(prefix):]}" if url.startswith(prefix) else url


if settings.database_url.startswith("sqlite"):
    _kw = {"connect_args": {"check_same_thread": False}}
    if settings.database_url in ("sqlite://", "sqlite:///:memory:"):
        _kw["poolclass"] = StaticPool  # one shared connection, or each session would see its own empty database
else:
    _kw = {}
engine = create_engine(_resolve(settings.database_url), **_kw)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)


def session() -> Session:
    return Session(engine)
