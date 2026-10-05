import os

os.environ["PYTH_MOCK_PRICE"] = "2650"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["VENUE"] = "simulated"
os.environ.setdefault("API_KEY", "test-key")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """Empty tables for every test (the in-memory engine is shared)."""
    from sqlmodel import SQLModel

    from app import models  # noqa: F401
    from app.db import engine

    SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)
    yield
