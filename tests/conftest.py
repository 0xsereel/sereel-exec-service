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
