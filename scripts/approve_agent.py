import os
import sys
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

URL = constants.TESTNET_API_URL
master = Account.from_key(os.environ["HL_MASTER_KEY"])
info = Info(URL, skip_ws=True)


def show_balances():
    perps = info.user_state(master.address)["marginSummary"]["accountValue"]
    spot = next(
        (b for b in info.spot_user_state(master.address)["balances"] if b["coin"] == "USDC"),
        {"total": "0", "hold": "0"},
    )
    print(f"address: {master.address}")
    print(f"perps:   {perps} USDC")
    print(f"spot:    {spot['total']} USDC (on hold: {spot['hold']})")
    return float(perps), float(spot["total"])


perps, spot = show_balances()
ex = Exchange(master, URL)

# Move spot USDC to perps:  python3 scripts/approve_agent.py --to-perp 300
if "--to-perp" in sys.argv:
    amount = float(sys.argv[sys.argv.index("--to-perp") + 1])
    print("spot -> perps:", ex.usd_class_transfer(amount, True))
    show_balances()

# Create a NEW API wallet (replaces the existing "sereel" one):  --approve
if "--approve" in sys.argv:
    result, agent_key = ex.approve_agent("sereel")
    print(result)                          # expect status "ok"
    print("HL_API_WALLET_KEY =", agent_key)