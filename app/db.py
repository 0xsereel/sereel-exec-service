from sqlmodel import Session, SQLModel, create_engine

from .config import settings

_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=_args)


def init_db() -> None:
    from . import models  # noqa: F401  (register tables)

    SQLModel.metadata.create_all(engine)


def session() -> Session:
    return Session(engine)
