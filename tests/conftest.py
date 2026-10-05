import os

os.environ["PYTH_MOCK_PRICE"] = "2650"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["VENUE"] = "simulated"
os.environ.setdefault("API_KEY", "test-key")
