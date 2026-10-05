import os
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

URL = constants.TESTNET_API_URL
master = Account.from_key(os.environ["HL_MASTER_KEY"])

info = Info(URL, skip_ws=True)
print("balance:", info.user_state(master.address)["marginSummary"]["accountValue"])

ex = Exchange(master, URL)
result, agent_key = ex.approve_agent("sereel")
print(result)                          # expect status "ok"
print("HL_API_WALLET_KEY =", agent_key)