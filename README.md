# sereel-exec-service

FastAPI execution service for Sereel delta-neutral hedges. It takes stablecoin funding from a manager's Solana wallet,
runs the hedge on **Hyperliquid testnet** (`xyz:GOLD`, a HIP-3 market), reports value back, attests every action on **Solana
devnet**, pays out stablecoins on schedules, and ships a testnet market maker. Cantina calls it by base URL and API key.

**Networks:** Hyperliquid testnet and Solana devnet only. The service refuses to start against mainnet unless
`ALLOW_MAINNET=true`, and the market maker refuses mainnet no matter what.

## Quick start (clean machine to a running demo)

```bash
# 1. install (Python 3.11+)
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"

# 2. configure: copy and fill in .env (see .env.example for every setting)
cp .env.example .env
#    required: API_KEY (any long random string), PYTH_API_KEY, and the Hyperliquid testnet values below

# 3. keys, database, mint: prints ONLY the funding wallet address, then waits
sereel init
#    -> send devnet SOL to that address at https://faucet.solana.com (choose Devnet); `init` then distributes SOL to the
#       other wallets, creates the stablecoin mint, runs `alembic upgrade head`, and checks Hyperliquid.
#    -> BACK UP keys/ and .env somewhere outside this repository now.

# 4. run the API (with the market maker alongside, on testnet)
sereel serve --mm            # http://localhost:8000

# 5. expose it to Cantina
ngrok http 8000              # paste the https URL and API_KEY into Cantina > Settings > Integrations
```

**Hyperliquid testnet setup (once).** Two accounts: the **master** (holds margin) and a second **market-maker** account.
For the master: fund it with testnet USDC (the faucet only works for accounts with mainnet deposit history, so fund it from
another account), approve an **API (agent) wallet** for it, and set `HL_ACCOUNT_ADDRESS`, `HL_API_WALLET_KEY` and, for
development only, `HL_MASTER_KEY` (moving margin to the `xyz` dex is a user-signed action; see the permission model).
For the market maker set `HL_MM_ACCOUNT_ADDRESS` and `HL_MM_API_WALLET_KEY` (its own key works) and put USDC in it. Check
everything with `sereel init`: it prints the resolved `xyz:GOLD` asset id (750003 on testnet), mark and oracle, the margin on
the dex, and each account's mode.

**What to expect when you run it.**
- `GET /health` (no key) shows the leverage assertion for `xyz:GOLD` (`ok: false` until the first order sets it to 3x), the
  reconciliation, and `unresolved_transfers` / `unresolved_withdrawals` (both should be 0).
- A strategy is created with `POST /strategies`; the client then sends a transfer **with the intent id as the memo**; about
  15-30 seconds later (finalization) the strategy is `active` with a live short. See "Strategies and funding intents".
- **The market maker must be running** for orders to fill on this thin testnet book. Without it, deploy, rebalance and close
  fail with `NO_LIQUIDITY` and send nothing (see below).

**Day-to-day commands**

| Command | What it does |
|---|---|
| `sereel serve [--mm]` | the API, the payout scheduler, the deposit watcher, the minute P&L snapshots; `--mm` also runs the market maker |
| `sereel mm run --market XAU-HL` / `mm stop` | the market maker on its own (stop = cancel quotes, flatten reduce-only) |
| `sereel strategies list [--all] [--json]` | strategies with position, value and margin health (asks the running API) |
| `sereel strategies set-owner <id> --pubkey/--multisig` | operator: bind an owner to an ownerless strategy |
| `sereel strategies retry-withdrawal <id> [--confirm-not-sent]` | operator: resume a failed withdrawal |
| `sereel payouts new / run / list / pause / resume / stop / send` | scheduled and one-off stablecoin payouts |
| `alembic upgrade head` / `alembic revision --autogenerate -m "..."` | database migrations |
| `pytest` | the test suite (no network needed; it uses a simulated venue and a fake chain) |

**Signing from a client.** Every call that moves nothing on Solana (edit, rebalance, close, return excess, change owner) carries
a signed message. The exact format, with test vectors a client author can check byte for byte, is in "Signed-message
authorization and strategy ownership". For local development without a signer, `DEV_AUTH_BYPASS=true` skips it (off
mainnet only, logged on every request).

**Troubleshooting**
- `BAD_REQUEST` on create with "below the venue's $10 minimum order": Hyperliquid rejects any order under $10 notional, so a
  strategy whose hedge (`target_exposure_units` x `hedge_ratio_bps`) is worth less is refused up front (`MIN_ORDER_USD`, with a
  5% cushion for the mark moving). The message gives the smallest exposure that works. At the testnet gold price that is
  roughly $3.5 of margin at 3x.
- `NO_LIQUIDITY`: nothing is offering the size you need. Start the market maker (`sereel serve --mm`, or `sereel mm run`).
  On testnet the market maker is effectively the only liquidity; once it is stopped the book can be empty.
- `STALE_PRICE` / `PRICE_SOURCE_AUTH`: Pyth is unreachable or rejected `PYTH_API_KEY`. A *closed* market is not an error (see
  "Closed markets").
- `/health` shows `reconciliation.ok: false`: the venue holds less than the strategies' ledger says. See "Reconciliation".
- A funded strategy stuck in `pending_funding` with a `failure_reason`: it is retrying (the reason says why); after
  `ACTIVATION_GRACE_S` it fails and the funding is refunded to the sender.
- **Startup is slow, or fails with `VENUE_UNAVAILABLE` ("Hyperliquid did not answer while loading market metadata").**
  Starting up downloads Hyperliquid's market metadata, which is large; on a slow testnet that can take a minute (a cold start
  took 49 s when Hyperliquid answered in 7-11 s per call). There is **one** download shared by every client, it uses
  `HL_CONNECT_TIMEOUT_S` (default 60 s) and is retried `HL_CONNECT_ATTEMPTS` (default 4) times with a growing pause. Raise
  those if your connection is worse; the service then says which step failed and after how many attempts.
- **A process that will not exit** (a server or market maker stuck in shutdown): the Hyperliquid SDK defaults to **no request
  timeout**, so a silent connection blocks a thread forever. Once connected, every Hyperliquid client here drops to
  `HL_REQUEST_TIMEOUT_S` (default 15 s), and a test fails if any new client is created without a timeout. Solana and Pyth
  calls already have timeouts.
- Hyperliquid or Pyth being slow (seconds per call) delays everything and can leave gaps between the market maker's
  cancel-and-replace cycles; give it time or use `sereel mm run --center mark`.

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
- **Action quota:** Hyperliquid allows an account 10,000 requests plus 1 per USDC of volume it has traded. The maker checks
  every 5s but only sends a cancel + order batch when a resting quote has drifted more than `--requote-bps` (default 3)
  from where it would be quoted now, or a level was filled or inventory changed the skew. If the venue still answers
  `Too many cumulative requests`, the maker logs one error, pauses quoting for 5 minutes, and then retries. While it is
  paused nothing rests on the book, so strategy deploys fail with `NO_LIQUIDITY`. Recover by trading taker volume on the MM
  account (about $1 per request over the limit) or by pointing `HL_MM_*` at a fresh account.
- **Center:** the Hyperliquid **oracle** price by default (`--center oracle|pyth|mark`). Testnet marks drift from the
  oracle (observed: mark 4211 vs oracle 4160); centering on the oracle pulls the mark back. A quote that would
  cross the book is sent GTC and takes the stale liquidity; a quote that rests safely is post-only (ALO). In a live
  run the mark-oracle gap fell from +51 to about +4..+7 while quoting, and re-opened after the maker stopped.
- **Inventory control:** quotes skew against inventory (long: both sides lower; short: both sides higher, up to
  `skew_bps` at the inventory limit), and the side that would grow inventory past `max_inventory` is dropped.
- **Startup:** if the account still holds inventory from a previous run, the maker cancels stale quotes and flattens it
  **reduce-only before quoting normally**, as soon as the book has anything to take it. It waits up to `--flatten-wait`
  seconds (default 60); if the book never allows it, it starts quoting anyway, skewed against the inventory.
- **Shutdown:** Ctrl-C, SIGTERM or `sereel mm stop` cancels all quotes and flattens the position with a
  **reduce-only IOC**. With `sereel serve --mm` the maker runs inside the server (no pidfile, so `mm stop` never signals the
  server) and is stopped when the server stops.
- **Limit of the flatten.** On this testnet the MM is effectively the only liquidity. Once it cancels its quotes the book
  can be empty, so the flatten may leave residual inventory (seen live: 0.0719 oz long left because no bid existed at
  any price near the market). That is expected, not a bug: restart the MM and it trades out (long inventory skews its
  quotes down). The same applies to **closing a strategy: the market maker must be running to take the other side.**

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

## Strategies and funding intents

A strategy is a delta-neutral hedge: the service shorts `target_exposure_units x hedge_ratio` of the market on the
venue against a fund's exposure. Funding is by **intent**; the client never submits a signature.

1. `POST /strategies` registers the strategy and its intent (`POST /strategies/intents` is accepted as an alias, the name the
   pre-v4 plan used; without it that path reads as "strategy id = intents" and answers 405): status `pending_funding`, with `intent_id`,
   `funding_address` and `expires_at`. It is rejected (400) if `expected_amount_usd` is more than 2% below the required
   margin, which the service computes itself: `short size x price / leverage x (1 + MARGIN_BUFFER_PCT)`.
2. The client sends a stablecoin transfer to the (single, global) funding address with an SPL **Memo that is exactly the
   `intent_id`**, from the `registered_sender_address` (the wallet itself, or the Squads vault PDA for a multisig).
3. The **deposit watcher** (every 5s) reads **finalized** transactions only and applies these rules:

| Transfer | Result |
|---|---|
| memo = intent **and** sender = registered sender, intent open | credited. Under: stays `pending_funding`, `received_amount_usd` and `shortfall_usd` shown, later transfers add up. Exact or over: activates, and the excess is margin. |
| anything else: no memo, unknown memo, **wrong sender**, intent cancelled / expired / already active | **refunded to its sender and attested**, never credited |
| from the service's own addresses (funding, attest, payment source, mint authority) | ignored |

   Expiry is 1 hour for a wallet and 7 days for a multisig vault (a Squads vault is a PDA, i.e. off the ed25519 curve,
   so no hint field is needed). A deposit that finalizes after expiry or cancel is refunded; partial funding is
   refunded when an intent expires or is cancelled.
4. **Activation**: margin is raised on the venue, leverage is set (see below), and the short is opened with IOC orders.
   Transient failures (price deviation, stale price, no fill, no book) are retried every tick for `ACTIVATION_GRACE_S` seconds (default 120) measured from when funding completed;
   after that the strategy is `failed` and the funding is refunded. A partial fill activates with the size actually
   filled. Activation is **held, not failed**, if the venue position differs from the ledger by more than one size
   step (a position the ledger cannot explain): trading on top of it would compound the error.
5. Every deploy, deposit and refund is attested: an SPL memo `{"v":1,"id","fund","a":<action>,"net","h":<sha256 of the
   full record>}`; the full record is in the database (`action` table).

`POST /strategies/{id}/deposits` (top-up) uses the same mechanism with its own `intent_id`, credits margin only
(no trade), and `POST /strategies/{id}/cancel` cancels a pending intent (409 once it is active, expired or cancelled).
Tenant scoping: send `X-Sereel-Org` and `X-Sereel-User`; another org's strategy is a 404.

**Watcher guarantees.** The cursor (newest finalized signature fully processed) is stored in the database and the
baseline is taken at API startup, before any intent can exist, so a restart neither skips nor repeats a transfer and
old wallet history is never mistaken for a deposit. Each transfer is claimed in the database (primary key) before
anything is done with it, so none is credited or refunded twice, even with two workers. A transfer left half-handled
by a crash is marked `refund_unconfirmed` and **never retried automatically**; `/health` reports
`unresolved_transfers` (failed or unconfirmed refunds), which need a person.

**Leverage.** Before the first order the service sets the market to `max_leverage` from `markets.yaml` (3x):
isolated if the venue accepts it, else cross, verified by reading it back (`LEVERAGE_NOT_SET` and no order if it cannot
be confirmed). `/health` asserts it: `hyperliquid.leverage.<market> = {configured, actual, mode, ok}`.

**Reduce-only.** An order that only shrinks the account position (closes, rebalance-downs) is sent reduce-only; opening
and growing are not. A trade that would flip through zero is not reduce-only.

**Closed markets.** Pyth feeds carry a market-hours `schedule` (timezone, weekly hours, holidays). If the price is stale
and the schedule says the market is closed, the service uses the last Pyth price and sets `market_closed: true`
instead of failing with `STALE_PRICE`, so the demo works outside gold trading hours.

**Margin reaches the venue exactly.** When a deposit is credited the service moves *exactly that amount* onto the market's dex
balance (`add_margin`); it never tops the dex up "to a target". (An earlier version did, measured against `accountValue`,
which includes unrealized P&L; that moved less cash than it credited and, when the strategy later left, the other
strategies' pot covered the difference. It was found as a 43-cent shortfall on testnet, hidden behind a cash-based check.)

**Reconciliation.** `/health` reports `reconciliation`. It compares **equity**: the venue's `accountValue` against the sum of
what the strategies are entitled to (credited margin + realized P&L + funding - fees + unrealized P&L at the mark),
`difference_usd` = venue minus ledger. It does **not** compare cash, because Hyperliquid realizes a close against the
blended *account* entry while the ledger realizes it against each strategy's own entry: the cash figures legitimately differ,
and the amount reappears in unrealized P&L, so in equity terms it cancels exactly. The ledger is marked at the price
*implied by the same Hyperliquid snapshot* as `accountValue` (entry + unrealized / size), not at a separate mark read, because
two reads seconds apart differ by (position size x the price move between them) and look like drift. `ok` is false only if the
venue holds **more than 5 cents less** than the ledger; surplus venue cash (dust that belongs to no strategy) is not an
alarm. The tolerance covers rounding only: fees, realized P&L and unrealized P&L are accounted for exactly.

**Liquidation price.** `position.liquidation_price_usd` is the nearer of the service's own figure (assuming all of the
strategy's margin backs the position) and the venue's reported liquidation price for the shared account position, so it
is never rosier than the venue's.

**Migrations on start.** `MIGRATE_ON_START` (default `true`) makes the API run `alembic upgrade head` at startup. Set
it to `false` in production: the API then refuses to start unless the database is already at the latest revision, and
you run `alembic upgrade head` yourself.

## Editing, rebalancing, value and history

All the signed calls below take the string-only params described in the authorization section, and the body fields must be
the same strings that were signed; a JSON number in the body is rejected (`AUTHORIZATION_INVALID`) before anything is stored.

- **`PATCH /strategies/{id}`** (`edit_hedge_settings`): changes `hedge_ratio_bps` and/or `target_exposure_units` (both
  strings). It moves the **target only**; no order is sent. It recomputes `required_margin_usd` and returns the strategy
  with `hedge_gap_units` / `hedge_gap_bps` (target short minus current short; positive means under-hedged). Active
  strategies only.
- **`POST /strategies/{id}/rebalance`** (`rebalance`, params `{}`): trades the hedge to its target if the gap is beyond the
  strategy's `rebalance_band_bps` (a gap exactly equal to the band does not trade); **`?force=true`** trades regardless.
  Shrinking is **reduce-only**; growing is not, and needs the strategy's own capital (margin + realized P&L + funding -
  fees) to cover the new size at its leverage, else `INSUFFICIENT_MARGIN` ("add margin with a top-up"). A within-band call
  trades and attests nothing (the signed request is still recorded). A partial fill leaves the remaining gap visible;
  nothing filled is `ORDER_NOT_FILLED` and leaves the strategy untouched. It is held (409) while the venue position and
  the ledger disagree. A crash mid-rebalance is recovered at startup.
- **The fill ledger:** growing blends the entry price; shrinking realizes P&L on the part closed at the unchanged entry
  (a short gains when the price fell); flipping through zero realizes the closed part and reopens at the fill price.
- **Funding** is paid on the shared account position, so each strategy is allocated the share equal to its size over the
  account's size, booked before any size change and every minute, with a cursor so a payment is never counted twice.
  `position.funding_paid_usd` is positive when paid; `/value`'s `funding_usd` is the net received (the sign flips).
  *Not yet seen live: the funding rate of `xyz:GOLD` has been 0, so the real funding-history entry shape is unverified.*
- **`GET /strategies/{id}/value`**: `{margin_usd, unrealized_pnl_usd, realized_pnl_usd, funding_usd, fees_usd, value_usd,
  hedge_pnl_usd, as_of, attestation_sig, attestation_url, market_closed}`. `value_usd = margin_usd + unrealized_pnl_usd`
  (the literal v4 definition); `hedge_pnl_usd = unrealized + realized + funding - fees` and **never includes margin**. All
  five components are independently populated. A strategy with no position reports zeros; a venue outage is
  `VENUE_UNAVAILABLE` (503), never a wrong number.
- **`GET /strategies/{id}/value?as_of=<time>`** (ISO 8601, a naive time is UTC, or Unix milliseconds): the stored P&L
  snapshot at or just before that time. Snapshots are taken **every minute** and **after every action** (deploy, deposit,
  edit, rebalance), so history is never recomputed. `as_of` in the response is the snapshot's own time. Before the first
  snapshot it is a 404. `attestation_sig` is the latest attestation in force at that moment.
- **`GET /strategies/{id}/history`**: every action, oldest first: type, time, who signed it, the fill (size, average
  price, remaining), fee, realized P&L, Hyperliquid order ids, the Solana signature (for `deploy`, the funding
  transfer), the attestation signature and its explorer URL, and the full record.
- **Numbers** in responses are JSON numbers rounded to 8 decimals.

## Closing a strategy and returning funds (withdrawals)

Two ways money leaves a strategy, both signed (see the authorization section) and both modelled as one **withdrawal**:

- **`DELETE /strategies/{id}`** (`close_strategy`; the body carries `destination_wallet_address` and `authorization`):
  closes the position and returns everything: margin + P&L - fees +/- funding.
- **`POST /strategies/{id}/withdrawals`** (`return_excess`; `amount_usd` and `destination_wallet_address` as strings): returns
  part of the margin and **keeps the hedge open**. Allowed only while the equity left stays at least **1.5x the required
  margin**; otherwise `WITHDRAW_BELOW_MARGIN` with the maximum. The amount is reserved immediately, so two requests cannot
  together exceed the cap. `type` may be sent, but only `return_excess` is accepted here.

Both return a **StrategyWithdrawal** at once (`status: requested`) and a background step machine does the work; poll
`GET /strategies/{id}/withdrawals/{wid}` (also listed at `GET /strategies/{id}/withdrawals`). A retried request (the same
signed authorization and content) returns the same withdrawal instead of creating another.

```
requested -> position_closed -> released -> bridging -> completed          (failed from any of the first four)
```

| Step | What happens |
|---|---|
| `requested` | close: **check the book**, then close with reduce-only IOCs (retried); excess: nothing to close |
| `position_closed` | USDC moves from the market's dex balance back to the main balance (master-signed on a `default` account) |
| `released` | bridge to Solana: testnet `MirroredRoute` moves nothing; production `CctpHyperliquidRoute` is a stub |
| `bridging` | the destination is paid on Solana from the funding wallet (on devnet, minting if the wallet is short); attested |

For a close the final `amount_usd` is the ledger's finalized figure (the amount at `requested` is an estimate). The
attestation covers the close fills, release, route and payout signature.

**Liquidity.** The same rule applies to **close, rebalance and deploy**. Before sending anything, an order checks the book: it needs the strategy's whole size offered within the
IOC's 0.5% slippage band of the mark. If not, the request fails with **`NO_LIQUIDITY`** (HTTP 409), says how much is
offered and how much is needed, tells you to start the market maker, **sends nothing, and does not spend the signature**
(the same signed request works once the book is back). The check runs again when the step executes, so a book that
empties in between fails the withdrawal cleanly (the strategy goes back to `active`) instead of retrying into nothing.
Partial closes keep the strategy `closing` with the reduced size; closing again finishes it.

- **Rebalance** checks only when a trade would actually happen (a within-band or no-op call needs no book), for the size it
  would trade, before the signature is spent and again under the account lock.
- **Deploy** has no waiting client, so a funded strategy that finds no book keeps its funds and retries on the next watcher
  ticks (`failure_reason` says `NO_LIQUIDITY ... retrying, attempt n`) with **no order sent and no margin moved**; after
  `ACTIVATION_GRACE_S` seconds (default 120, measured from when funding completed, so it does not depend on how slow the venue is
  or how many ticks run) it fails and the funding is refunded. `failure_reason` counts down ("gives up and refunds in 87s").
  Deploy needs the whole target size, like close.

**Failure rules.**
- Failed **before anything was traded**: the strategy returns to `active`.
- `release` and `bridge` retry on transient errors (5 attempts, one per watcher tick), then fail with `needs_operator`.
- The **Solana payout is never retried automatically**: a timeout can still land. It fails with `needs_operator`, `/health`
  counts it in `unresolved_withdrawals`, and closing again is refused (it would pay twice). After you have checked the
  funding wallet's transactions (memo `sereel <type> <id>`), run
  **`sereel strategies retry-withdrawal <id> --confirm-not-sent`**. If the signature is already recorded (the money went out
  and a later step failed), the retry only finishes the withdrawal and never sends again. A failed excess withdrawal
  returns its reserved margin to the strategy; the operator retry reserves it again.
- A crash mid-withdrawal resumes at the persisted step on the next watcher tick.

## Signed-message authorization and strategy ownership

Strategy actions that move nothing on Solana (edit hedge settings, rebalance, close, return excess, change owner) are
authorised by a **signed message**, not just the API key. There is no callback to another backend: the strategy itself
records who may manage it.

**Owner binding.** `POST /strategies` must carry exactly one of:
- `owner_pubkey`: the manager's Sereel Solana wallet, or
- `owner_multisig`: the address of a **Squads v4 multisig account** (the account that holds the member list, *not* the
  vault PDA, which does not contain its members and cannot be traced back to them). The service reads the account
  on-chain at creation and rejects anything that is not a Squads multisig.

It is stored on the strategy, returned in the strategy object, and included in the creation record and the deploy
attestation. This is an addition to the v4 create body, so the frontend must send it.

**The message** (Cantina v4 contract):

```
sereel-strategy-v1|solana|<wallet_pubkey>|<action>|<strategy_id>|<params_hash>|<nonce>|<timestamp_ms>
```

- `wallet_pubkey`: the signer's base58 Solana public key. `action`: one of the table below. `strategy_id`: the `{id}` in
  the path. `nonce`: a fresh UUID v4 per request. `timestamp_ms`: Unix **milliseconds** (`Date.now()`), not seconds.
- `signature`: a raw **ed25519** signature over the UTF-8 bytes of the message (`nacl.sign.detached`), base58-encoded.
  It is not a Solana transaction signature; there is no transaction.
- The request carries `authorization: {message, signature, nonce, timestamp, publicKey}` (`timestamp` a JSON integer).

### Canonical params format (the exact spec for clients)

`params_hash` is the **lowercase hex SHA-256 of the canonical params text**, encoded as UTF-8. The params are an object
holding exactly the fields the action lists below, *with the values the request body carries*, and nothing more.

1. **A flat object whose values are all strings.** There are no nested values, numbers, booleans or nulls in signed params.
2. **Sorted keys.** Keys are sorted (by Unicode code point).
3. **No whitespace** anywhere: `{"a":"1","b":"2"}`, never `{ "a": "1", "b": "2" }`.
4. **No JSON numbers, anywhere, for any field.** This includes whole numbers: `hedge_ratio_bps` is sent and signed as
   the string `"6000"`, and the JSON number `6000` is **rejected with `AUTHORIZATION_INVALID`**. This is deliberate: two
   clients can print the same number differently (`520.0` vs `520`, `1e-7` vs `0.0000001`) and would sign different
   bytes; a string is the same bytes everywhere.
5. **Amounts, prices and quantities are decimal strings:** `"520.5"`. A decimal string is digits with an optional
   fractional part of 1 to 18 digits: no sign, no exponent, no leading zeros, no trailing dot (`"0"`, `"1250.75"`,
   `"0.000001"` are valid; `"+1"`, `"-1"`, `"1e3"`, `"01"`, `"1."`, `".5"` are not). The string is hashed exactly as
   sent: `"520"` and `"520.0"` are different strings and hash differently.
6. **Whole numbers (basis points) are digit strings:** `"6000"`. Digits only, at most 15, no sign, fraction or leading
   zeros (`"0"` is valid; `"06000"`, `"6000.0"`, `"-1"` are not).
7. **Strings** are escaped as `JSON.stringify` does (`"`, `\`, control characters as `\n` / `\u00XX`); non-ASCII
   characters are written as is (UTF-8), not as `\uXXXX`.
8. The server hashes the values it read from the **raw request body**, so what you hash must be what you send.

Field types (anything else is not signable and is rejected):

| Field | Type |
|---|---|
| `target_exposure_units`, `amount_usd` | decimal string, e.g. `"520.5"` |
| `hedge_ratio_bps` | whole-number string, e.g. `"6000"` |
| `destination_wallet_address`, `owner_pubkey`, `owner_multisig` | string |

Actions, their endpoints, and exactly which fields are hashed (only those present in the body):

| Action | Endpoint | params |
|---|---|---|
| `edit_hedge_settings` | `PATCH /strategies/{id}` | `hedge_ratio_bps` (whole-number string), `target_exposure_units` (decimal string) |
| `rebalance` | `POST /strategies/{id}/rebalance` | `{}` |
| `return_excess` | `POST /strategies/{id}/withdrawals` | `amount_usd` (decimal string), `destination_wallet_address` |
| `close_strategy` | `DELETE /strategies/{id}` | `destination_wallet_address` |
| `change_owner` | `POST /strategies/{id}/owner` | one of `owner_pubkey` / `owner_multisig` |

Note: the v4 draft sent `hedge_ratio_bps` and `target_exposure_units` (PATCH) and `amount_usd` (withdrawals) as JSON
numbers. In the request body of these signed actions they must now be **strings**, and the same strings are what you sign.

### Test vectors

Check your implementation against these before talking to the service. Every vector uses the same signer, strategy,
nonce and timestamp; ed25519 signatures are deterministic, so you must reproduce the signature byte for byte.

```
ed25519 seed (hex)  000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f
publicKey           FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF
strategy_id         5b5e4c52-8f1a-4d0e-9a53-2f6f3e1c7a10
nonce               0b1f6c7e-3a2d-4c1b-9e8f-7d6c5b4a3921
timestamp           1759577234123
```

**Vector 1: `edit_hedge_settings`**

```
params (canonical)  {"hedge_ratio_bps":"6000","target_exposure_units":"520.5"}
params_hash         9ec3238301cff9109a844324fff25a476d690ff16ab22969bbb88e9ac4a4d9aa
message             sereel-strategy-v1|solana|FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF|edit_hedge_settings|5b5e4c52-8f1a-4d0e-9a53-2f6f3e1c7a10|9ec3238301cff9109a844324fff25a476d690ff16ab22969bbb88e9ac4a4d9aa|0b1f6c7e-3a2d-4c1b-9e8f-7d6c5b4a3921|1759577234123
signature (base58)  3BhmB6sWKXB3XUJdkd4Z7UUaacLYoVVZF7QSFuL8qoR6xfmUBmoYxCoD2gHhjUjexDzGTtPfy1h52frDTpxHhbSu
```

**Vector 2: `return_excess`**

```
params (canonical)  {"amount_usd":"1250.75","destination_wallet_address":"7VPsT9gYv64jKtqysgAxR6xofJCsGSP4DsPDNhtkGi1L"}
params_hash         e9b0a7572489a86d4db11cb1014485034d878633cf2869b029a09d84e68f8aa8
message             sereel-strategy-v1|solana|FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF|return_excess|5b5e4c52-8f1a-4d0e-9a53-2f6f3e1c7a10|e9b0a7572489a86d4db11cb1014485034d878633cf2869b029a09d84e68f8aa8|0b1f6c7e-3a2d-4c1b-9e8f-7d6c5b4a3921|1759577234123
signature (base58)  4Cosv4o7E2qi9UzCXzGymAy7Ji2xfnsxAH9cZinnPNXSXnkdC7hw3PHkpYjU9Daf7szsgqpBDY8W3XSsAwAm1oi
```

**Vector 3: `rebalance`**

```
params (canonical)  {}
params_hash         44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a
message             sereel-strategy-v1|solana|FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF|rebalance|5b5e4c52-8f1a-4d0e-9a53-2f6f3e1c7a10|44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a|0b1f6c7e-3a2d-4c1b-9e8f-7d6c5b4a3921|1759577234123
signature (base58)  3F4AH1d47V6hCVYuYb2GcVFei6SS2LXyjdVkGiRYrP73rpk8FtzMEEprzn4p6YabsU3dEH9D92H2VciztPi6PtoB
```

(The values above are checked against the implementation by a test, so they cannot go stale.)

**Verification, on every call.** The server rebuilds the message from the request (it never trusts the client's string),
verifies the signature, rejects a timestamp more than **60s old** or **30s ahead**, and rejects a nonce it has seen
(stored in the database, so a restart does not reopen replays; a bad signature never consumes a nonce). Then the signer
must be the strategy's `owner_pubkey`, or a **current member** of its `owner_multisig` (any member; the Squads account
is read on-chain and cached for `SQUADS_CACHE_S`, default 60s, so a removed member loses access within a minute). A
missing `authorization` is `AUTHORIZATION_REQUIRED` (401); anything wrong with it is `AUTHORIZATION_INVALID` (403).
Deploy and top-up need no signature: the on-chain transfer is their proof.

**Changing the owner** is itself a signed `change_owner` action by the *current* owner (or a member of the current owner
multisig), and it is attested like any other action. A strategy with no owner bound (one created before this existed)
cannot be authorised by any signature.

**`DEV_AUTH_BYPASS=true`** skips all of this for local development. It is refused at startup together with
`ALLOW_MAINNET=true`, is ignored at request time if `ALLOW_MAINNET` is true, prints a banner at startup, and logs a
`WARNING` on **every** request it lets through. Records made under it say `signed_by: dev-bypass`. Never set it outside
a dev machine.

*Tested against a real devnet Squads account* (captured in `tests/fixtures/`), not only synthetic data; the account
layout was checked against Squads' source (`state/multisig.rs`).

## Cantina contract (v4)

`tests/test_contract.py` encodes the v4 document field by field and runs it against the real API: the Strategy, position,
StrategyDeposit and StrategyWithdrawal objects, `/value`, the three status enums, `200` on every success (including
`DELETE`, which takes a body and returns a withdrawal), bare-array lists, ISO 8601 timestamps, money as JSON numbers, basis
points as integers, opaque string ids, `X-Sereel-Key` on every route but `/health`, 404 for another org's strategy, and the
non-2xx body `{"error", "code"}`. Mutating the API in any of those ways fails the suite.

**Deliberate deviations from the v4 document** (decided with the product owner; the frontend must follow):
1. `POST /strategies` requires `owner_pubkey` **or** `owner_multisig` (a Squads multisig *account*).
2. In the signed calls (`PATCH`, `POST .../withdrawals`) `hedge_ratio_bps`, `target_exposure_units` and `amount_usd` are
   **strings** in the body, and the same strings are signed. A JSON number is `AUTHORIZATION_INVALID`.
3. Additive fields (ignored by a tolerant client): on the strategy `owner_pubkey`, `owner_multisig`, `market_closed`,
   `failure_reason`, `hedge_gap_units`, `hedge_gap_bps`; on `/value` `hedge_pnl_usd`, `attestation_sig`,
   `attestation_url`, `market_closed`. Extra routes: `POST /strategies/{id}/owner`, `GET /strategies/{id}/deposits[/{did}]`,
   `GET /strategies/{id}/history`, `GET /strategies/{id}/withdrawals`, `GET /markets`.

`/health` and `/markets` were out of scope in v4, so they follow the original spec: `/health` has version, venue, the
Hyperliquid network and account margin, the Solana network, funding address, stablecoin mint and the active strategy and
schedule counts (plus the leverage assertion, reconciliation and unresolved counters); `/markets` lists each market with the
live Hyperliquid mark and Pyth price.
Each `/markets` row carries **`status`**, exactly `"active"` or `"coming_soon"` (lowercase; the frontend compares it
strictly, so a missing or differently-cased value shows as "Coming soon"). It comes from `enabled` in `markets.yaml` (default
true) and never from live prices, so a slow price feed cannot grey a market out; a market with `enabled: false` is listed as
`coming_soon` and the API refuses new strategies on it. The frontend's `StrategyMarket` type is `{market_id, symbol, venue_coin, max_leverage, mark_price_usd, pyth_price_usd,
market_closed, deviation_bps}`, and every row here carries exactly those fields (plus `status` and `price_stale`, which a tolerant
client ignores). **Prices are never null:** the type is non-nullable, so a failed price read does not become `null` in a row.
Instead the last good values are served for up to 10 minutes with `price_stale: true` and the reason in `error`; with no usable
price at all the whole call fails with a 503 `{error, code}` (the underlying code, e.g. `STALE_PRICE`, or `VENUE_UNAVAILABLE`).
`market_closed` is informational: a closed market is still `active` and still selectable (its last price is used).

## Known limitations

- **Production funding route is a stub.** `CctpHyperliquidRoute` (Solana USDC to CCTP to Arbitrum to the Hyperliquid bridge,
  and back) is not implemented; testnet uses `MirroredRoute`, which moves nothing. A production withdrawal fails clearly at
  the bridging step.
- **Hyperliquid bridge withdrawal is unverified live.** `withdraw_to_arbitrum` is implemented and unit-tested, but the testnet
  rejected it for an account funded by an internal transfer; to be re-verified with a bridge-funded account.
- **Funding payments are unverified live.** `xyz:GOLD`'s funding rate has been 0, so the real funding-history entry shape has not
  been seen; the code follows Hyperliquid's documented shape.
- **One account, a per-strategy ledger.** All strategies share one Hyperliquid account and one `xyz:GOLD` position. Production
  should use one subaccount or vault per fund (subaccounts need $100,000 of volume; legacy vaults cost 10,000 USDC, need a
  100 USDC deposit, a leader holding at least 5%, and pay the leader a 10% profit share).
- **Thin testnet liquidity.** The market maker is effectively the only liquidity, and it can leave residual inventory when
  stopped into an empty book. At high Hyperliquid latency its cancel-and-replace cycle leaves gaps with no quotes.
- **No automatic Solana payout retry** (by design: a timeout can still land), and a crash mid-handling marks a transfer
  `refund_unconfirmed` for a person to resolve.
- **Single process.** The deposit watcher, scheduler and withdrawal machine assume one running service per database.

## Error codes

Every non-2xx response body is exactly `{"error": "<human message>", "code": "<CODE>"}`; nothing else. The codes
below are all of them (registry: `app/errors.py`; a test keeps this table, the registry and the source in step).
Cantina handles the first four specially, so their spelling is fixed.

| Code | HTTP | Meaning |
|---|---|---|
| `AUTHORIZATION_REQUIRED` | 401 | a signed-message `authorization` is missing on a call that needs one |
| `STALE_PRICE` | 503 | the Pyth price is older than the market's max_staleness_s while the market is open, or Pyth is unreachable |
| `PRICE_DEVIATION` | 400 | the venue mark differs from the Pyth price by more than MAX_PRICE_DEVIATION_BPS; no order was sent |
| `INSUFFICIENT_MARGIN` | 400 | not enough margin on the venue (or in the account) for the requested position |
| `AUTHORIZATION_INVALID` | 403 | the signed-message authorization failed verification (bad signature, wrong params, expired or replayed) |
| `CHAIN_UNAVAILABLE` | 503 | a Solana read needed for the decision (e.g. a Squads multisig's members) failed |
| `UNAUTHORIZED` | 401 | missing or invalid X-Sereel-Key |
| `BAD_REQUEST` | 400 | malformed or invalid input |
| `NOT_FOUND` | 404 | unknown id or route |
| `CONFLICT` | 409 | the request conflicts with the current state (e.g. cancelling an already active strategy) |
| `HTTP_ERROR` | n/a | any other HTTP error (the status is the HTTP status of the error, e.g. 405) |
| `INTERNAL` | 500 | unexpected error (details are logged, never returned) |
| `NOT_CONFIGURED` | 503 | a required setting is missing (e.g. STABLECOIN_MINT) |
| `UNKNOWN_MARKET` | 404 | the market id is not in markets.yaml (or not found on the venue) |
| `NO_LIQUIDITY` | 409 | the order book does not offer enough size within slippage of the mark to close the position; nothing was sent (start the market maker and retry) |
| `ORDER_NOT_FILLED` | 400 | no fill after the IOC retries, or the venue rejected the order |
| `VENUE_UNAVAILABLE` | 503 | the venue could not be reached or returned no price, so a live value cannot be computed |
| `LEVERAGE_NOT_SET` | 503 | the venue did not confirm the configured leverage for the market, so no order was sent |
| `VENUE_NOT_CONFIGURED` | 503 | Hyperliquid credentials are not set |
| `PRICE_SOURCE_AUTH` | 502 | Pyth Hermes rejected the credentials (PYTH_API_KEY) |
| `WITHDRAW_BELOW_MARGIN` | 400 | the withdrawal would leave the strategy below its required margin, or exceeds the withdrawable balance |
| `WITHDRAW_NOT_AUTHORIZED` | 503 | withdrawing needs the master key, which is not configured |
| `WITHDRAW_FAILED` | 400 | the bridge rejected the withdrawal |
| `PAYMENT_FAILED` | 502 | the Solana payout failed |
| `MM_MAINNET_REFUSED` | n/a | the market maker refuses to run unless HL_API_URL is testnet |
| `MM_SIZE_OUT_OF_RANGE` | n/a | market maker order size outside MM_MIN_SIZE..MM_MAX_SIZE |
| `MM_BAD_CONFIG` | n/a | invalid market maker settings |

Notes:
- **`AUTHORIZATION_REQUIRED` / `AUTHORIZATION_INVALID`** are emitted by the signed-message check (today on
  `POST /strategies/{id}/owner`; PATCH, rebalance, withdrawals and close use the same check as they are added). A bad or missing `X-Sereel-Key` is `UNAUTHORIZED`, which is
  separate.
- **There is no `MARKET_CLOSED` code, on purpose.** When Pyth's schedule says the market is closed, the service uses
  the last Pyth price and flags it (`market_closed: true` on the response) instead of failing with `STALE_PRICE`, so
  the demo works outside gold trading hours. `STALE_PRICE` therefore means "the market is open but the price is old,
  or Pyth is unreachable". The `PRICE_DEVIATION` check still applies against that last price.
- `UNKNOWN_MARKET` is 404 from the venue lookup and 400 when a request names an unknown market in its body.
