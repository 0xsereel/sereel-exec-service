"""Alembic environment. The database URL and metadata come from the app (DATABASE_URL, app.models)."""
from alembic import context
from sqlalchemy import create_engine, pool
from sqlmodel import SQLModel

from app import models  # noqa: F401  (registers every table on SQLModel.metadata)
from app.db import database_url

config = context.config
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(url=database_url(), target_metadata=target_metadata, literal_binds=True,
                      dialect_opts={"paramstyle": "named"}, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    # render_as_batch: SQLite cannot ALTER most things in place, batch mode recreates the table instead
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    shared = config.attributes.get("connection")  # in-memory databases (tests) hand us their one connection
    if shared is not None:
        _migrate(shared)
        return
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        _migrate(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
