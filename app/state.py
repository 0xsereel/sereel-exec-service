from .config import load_markets


class State:
    def __init__(self):
        self.markets = load_markets()
        self.venue = None


state = State()
