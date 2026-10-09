# sereel-exec-service

FastAPI service that runs Sereel delta-neutral gold hedges: it takes stablecoin from a manager's Solana wallet, shorts `xyz:GOLD` on
**Hyperliquid testnet**, attests every action on **Solana devnet**, and Cantina calls it over HTTP with an API key. It also has an AI
agent (setup chat, monitoring, optional autopilot rebalances) and an opt-in paid data feed (x402). Testnet/devnet only: it refuses
mainnet unless `ALLOW_MAINNET=true`, and the market maker refuses it always.

Everything not covered here (API, signing format and test vectors, error codes, migrations, agent and feed internals):
[docs/REFERENCE.md](docs/REFERENCE.md).

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"   # Python 3.11+
cp .env.example .env        # fill in API_KEY, PYTH_API_KEY and the Hyperliquid values (below)
sereel init                 # makes keys/, prints the funding address and waits for devnet SOL (faucet.solana.com)
sereel serve --mm           # API + deposit watcher + market maker on http://localhost:8000
ngrok http 8000             # give Cantina the https URL and API_KEY
```

`sereel init` also creates the mint, runs the migrations and checks Hyperliquid. **Back up `keys/` and `.env` outside the repo.**

**Hyperliquid (once):** two testnet accounts. The *master* holds margin: set `HL_ACCOUNT_ADDRESS`, `HL_API_WALLET_KEY` (an approved
API wallet) and, for dev only, `HL_MASTER_KEY`. The *market maker* is a second account: `HL_MM_ACCOUNT_ADDRESS`, `HL_MM_API_WALLET_KEY`.

**Check it works:** `curl localhost:8000/health` (no key) should show reconciliation `ok` and `unresolved_* = 0`. Create a strategy,
send USDC **with the intent id as the memo**, and about 15-30 s later it is `active` with a live short.

**Optional features** (all off by default, see `.env.example`): `AGENT_ENABLED=true` (+ `JEV_API_KEY`, `LLM_API_KEY`) for the AI
agent; `CUSTODY_PROOF_MODE=simulated` and `PUBLIC_URL=<your ngrok url>` for the data feed demo. `sereel agent key` prints the agent's
public key (what an owner grants autopilot to). `sereel x402 buy <id> --keypair buyer.json` is a demo buyer.

**Tests:** `pytest` (no network; simulated venue and fake chain; independent of your `.env`).

## Gotchas

**Running it**
- **The market maker must be running** (`serve --mm`) or orders cannot fill on the thin testnet book: deploy, rebalance and close fail
  with `NO_LIQUIDITY` and send nothing. It only re-quotes when the price moves; Hyperliquid limits an account to 10,000 requests plus
  1 per USDC traded, and an account over the limit is throttled until it trades more volume.
- **Startup can take about a minute** (Hyperliquid metadata is large; it retries). Migrations run on start (`MIGRATE_ON_START`); back up
  `service.db` first, and set it to `false` in production.
- ngrok's free domain needs the request header `ngrok-skip-browser-warning: true`.
- Pyth needs `PYTH_API_KEY`. A *closed* market is a flag on the response, not an error.

**Money and sizes**
- **Hyperliquid rejects orders under $10.** A hedge must be worth at least $10.50 (about 0.0027 oz at $4,000 gold); the API refuses a
  smaller strategy, rebalance or edit up front. A hedge under about $21 will work but autopilot may not be able to adjust it.
- **Funding is by intent:** the client sends the service's own USDC mint (`stablecoin_mint` from `GET /strategies/funding-address`)
  from the registered wallet to the funding address, memo = the intent id. Late, unmatched or cancelled deposits are refunded.
  A funded deploy that cannot open retries for 120 s, then fails and refunds.
- In the chat context Cantina sends, `wallets[].usdc_balance` must be that same mint's balance, not Circle's or another token.
- Margin is the manager's Solana wallet, never the service's Hyperliquid accounts.

**Signing**
- Every mutating call carries a signed message; **all signed params and bodies are strings** (`"6000"`, never `6000`). A signature is
  valid for 60 s and once only. `DEV_AUTH_BYPASS=true` skips it (never on mainnet). Format and test vectors: the reference.
- Only the owner can act. A delegate (the agent's key) can sign `rebalance` only, inside limits the owner signed; withdrawals,
  top-ups, close and the data feed always need the owner.

**AI agent and data feed**
- **Watching the agent:** Jev is asked on **every** cycle for every active strategy, whether or not anything is done (a quiet cycle is a
  decision of `none`). Each cycle logs the full Jev JSON and the decision to the server log and to `logs/agent_signals.jsonl`
  (rotates at 5 MB); startup prints `agent is ON ...` or `agent is OFF`; httpx and the 5 s scheduler lines are muted. Read it with `sereel agent log -n 20 [--strategy ID] [--follow] [--raw]`; `AGENT_LOG_STATE=true` also records the full
  text Jev was shown. The database keeps only the latest quiet cycle, so this file is the history.
- **Exposure updates** (`POST /strategies/{id}/exposure`, owner-signed `update_exposure {exposure_oz}`) move the target only: no trade, the agent
  rebalances (or proposes) on its next check. The chat can draft one ("bought 0.02 more"), but the server computes the new total, never the model.
- The model never chooses a number that reaches the venue; it only explains. The chat validates every value on the server and the
  draft is checked by the same code that deploys it.
- **Data feed:** off until the owner enables it. Payments settle straight to the customer (the service never holds them) in
  **Circle's devnet USDC**, a different mint from the service's own. Enabling creates the customer's token account and needs devnet
  SOL in the funding wallet. Executed-action attestations are sold only **3,600 s** after they happen: do not lower it.
- **Custody proofs are simulated** (labelled `simulated`, never attested, never called verified) until real zkTLS attestations exist.

**Keys**
- `keys/` and `.env` are git-ignored. `keys/agent.json` signs rebalances only and holds no funds. Never commit any of it.
