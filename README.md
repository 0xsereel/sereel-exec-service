# sereel-exec-service

FastAPI execution service for Sereel delta-neutral hedges: Solana devnet funding and attestations, Hyperliquid
testnet hedging (`xyz:GOLD`, a HIP-3 market), a testnet market maker, and payouts. *(Work in progress; sections are
added as each part lands.)*

## Hyperliquid account modes (abstraction)

Hyperliquid accounts run in one of several modes, readable with `{"type": "userAbstraction", "user": <address>}`.
The service detects the mode of each account **at startup and logs it**.

| Mode | Used for | How margin works | What the service does |
|---|---|---|---|
| `default` | **Master account** (holds strategy margin) | Spot, the main perp balance and **each builder dex (`xyz`) keep separate balances.** USDC must be moved to the dex before trading there. | `ensure_margin` moves the shortfall to `xyz`, drawing on main perp first, then spot, via `sendAsset`. `release_margin` moves it back. The `xyz` balance is what per-strategy margins are reconciled against. |
| `unifiedAccount` / `portfolioMargin` | **Market-maker account** | Spot USDC is shared collateral across spot and every perp dex. There are no per-dex balances to fund. | Dex transfers are **skipped** (they are rejected anyway: "Must deposit before performing actions"). `ensure_margin` only checks that spot USDC covers the amount. |

The master stays `default` on purpose: the explicit `xyz` balance is what makes the reconciliation check
(`sum(strategy margins) <= xyz dex balance`) meaningful. If the master is ever switched to a unified mode, the
service logs a warning at startup, skips transfers, and per-dex reconciliation is no longer meaningful.

Moving margin between the main balance and a builder dex is a **user-signed** action, so it needs the master key
(`HL_MASTER_KEY`, development only). API (agent) wallets can trade but cannot move or withdraw funds.

## Testnet market maker

`sereel mm run --market XAU-HL` quotes both sides of `xyz:GOLD` from a separate account (`HL_MM_*`) so hedge orders
have a counterparty on the thin testnet book. It refuses to start unless `HL_API_URL` contains `testnet`
(independent of `ALLOW_MAINNET`).

- **Sizes:** 0.02-0.05 per level (`MM_MIN_SIZE` / `MM_MAX_SIZE`), 3 levels per side by default.
- **Center:** the Hyperliquid **oracle** price by default (`--center oracle|pyth|mark`). Testnet marks drift from the
  oracle (observed: mark 4211 vs oracle 4160); centering on the oracle pulls the mark back. A quote that would
  cross the book is sent GTC and takes the stale liquidity; a quote that rests safely is post-only (ALO). In a live
  run the mark-oracle gap fell from +51 to about +4..+7 while quoting, and re-opened after the maker stopped.
- **Inventory control:** quotes skew against inventory (long: both sides lower; short: both sides higher, up to
  `skew_bps` at the inventory limit), and the side that would grow inventory past `max_inventory` is dropped.
- **Shutdown:** Ctrl-C, SIGTERM or `sereel mm stop` cancels all quotes and flattens the position with a
  **reduce-only IOC**.

## Permission model: who can do what

| Key | Can | Cannot |
|---|---|---|
| **Master** (`HL_ACCOUNT_ADDRESS`; dev key `HL_MASTER_KEY`) | Deposit, withdraw, approve API wallets, move margin to a builder dex | n/a. In production this key is controlled by the manager through Cantina's passkey (WebAuthn) wallet infrastructure, or by the fund's custodian, never by Sereel alone. |
| **API (agent) wallet** (`HL_API_WALLET_KEY`), approved by the master | Place and cancel orders, set leverage | Withdraw or move funds |

The service trades only with the API wallet. Moving margin and withdrawing are user-signed actions that need the master.

**Why an API wallet cannot withdraw:** `withdraw3` is a user-signed action that debits **only the signer's own
account**. It has no "on behalf of" field (unlike orders, which are signed by an agent for a named account), so an
API-wallet signature can only ever ask to withdraw from the API wallet's own, empty, account. It cannot move master
funds.

**Illustration (live, Hyperliquid testnet).** A `withdraw3` for $10 to the master's own
address, signed with `HL_API_WALLET_KEY` (an approved agent of the master), was rejected:

```
{'status': 'err', 'response': 'Must deposit before performing actions. User: 0xdb6e7d6594664a188e83e43816246b09e5a9d853'}
```

`0xdb6e…` is the **API wallet's own address**: Hyperliquid attributed the request to the signer and found no account
with a balance. The master's balance and ledger were unchanged. (The rejection is by account, not a literal
"agents cannot withdraw" message; the guarantee comes from the action's design, described above.)

## Withdrawals

`HyperliquidVenue.withdraw_to_arbitrum` is implemented (master-signed `withdraw3` to Arbitrum, destination defaults to
the master's own address, balance check, ledger lookup via `withdrawals_since`) and unit-tested.

**Live testnet verification: not yet done.** A $10 withdrawal from the master to its own address was rejected by
Hyperliquid with `Error withdrawing from bridge`, identically from the service's client and from a plain SDK client.
The master was funded by an internal spot `send` from another account plus a class transfer, **not by a bridge
deposit** from Arbitrum, which is the likely reason (unconfirmed: Hyperliquid documents no such rule). To be
re-verified with a bridge-funded account. Not blocking: the production return path is `CctpHyperliquidRoute`, which
remains a stub (Hyperliquid withdraw to Arbitrum, CCTP to Solana); on testnet `MirroredRoute` skips the bridge.

## Database and migrations

The schema is managed by **Alembic** (`migrations/`, baseline revision `baseline`). Nothing else creates or alters
tables. `DATABASE_URL` defaults to SQLite (`sqlite:///./service.db`, resolved against the project root) and is
Postgres-ready (`pip install -e ".[postgres]"`, `DATABASE_URL=postgresql+psycopg://...`).

- **`sereel init` runs `alembic upgrade head`**, and so does `sereel serve` on startup, so a database is always at
  the latest schema before anything uses it. You can also run it by hand: `alembic upgrade head`, `alembic current`.
- **Every schema change ships as a migration.** After changing `app/models.py`:
  `alembic revision --autogenerate -m "what changed"`, read the generated file (autogenerate misses some changes
  such as renames), and commit it with the model change. A test fails whenever the models and migrations have drifted
  apart, so a model change without a migration cannot pass CI.
- **SQLite** cannot alter tables in place, so migrations run in batch mode (the table is recreated); this is automatic.
- A database created before Alembic was added (tables present, no `alembic_version`) is stamped at the baseline and
  upgraded in place, keeping its rows.
- Roll back with `alembic downgrade -1` (or `base`). Back up the database first.
