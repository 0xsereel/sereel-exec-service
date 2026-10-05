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

1. `POST /strategies` registers the strategy and its intent: status `pending_funding`, with `intent_id`,
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
   Transient failures (price deviation, stale price, no fill) are retried every tick up to `MAX_ACTIVATION_ATTEMPTS`;
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

**Reconciliation.** `/health` reports `reconciliation`: the strategies' **ledger cash** (credited margin + realized P&L +
funding - fees) must not exceed the cash on the venue's dex balance (account value minus unrealized P&L). A breach is
logged as an error. Fees are accounted for exactly; the tolerance (5 cents) covers rounding only.

**Liquidation price.** `position.liquidation_price_usd` is the nearer of the service's own figure (assuming all of the
strategy's margin backs the position) and the venue's reported liquidation price for the shared account position, so it
is never rosier than the venue's.

**Migrations on start.** `MIGRATE_ON_START` (default `true`) makes the API run `alembic upgrade head` at startup. Set
it to `false` in production: the API then refuses to start unless the database is already at the latest revision, and
you run `alembic upgrade head` yourself.

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

1. **Sorted keys.** Object keys are sorted (by Unicode code point) at every level.
2. **No whitespace** anywhere: `{"a":"1","b":2}`, never `{ "a": "1", "b": 2 }`.
3. **Amounts, prices and quantities are decimal strings**, never JSON numbers: `"520.5"`, not `520.5`. A decimal string
   is digits with an optional fractional part of 1 to 18 digits: no sign, no exponent, no leading zeros, no trailing
   dot (`"0"`, `"1250.75"`, `"0.000001"` are valid; `"+1"`, `"-1"`, `"1e3"`, `"01"`, `"1."`, `".5"` are not). The string
   is hashed exactly as sent: `"520"` and `"520.0"` are different strings and hash differently.
4. **Whole-number fields** (basis points) are JSON **integers**: `7000`, never `7000.0` or `"7000"`.
5. **No floats anywhere.** A JSON number where a decimal string belongs, or a float where an integer belongs, is rejected
   with **`AUTHORIZATION_INVALID`**. This is deliberate: two clients can print the same number differently
   (`520.0` vs `520`, `1e-7` vs `0.0000001`) and would sign different bytes.
6. **Strings** are escaped as `JSON.stringify` does (`"`, `\`, control characters as `\n` / `\u00XX`); non-ASCII
   characters are written as is (UTF-8), not as `\uXXXX`.
7. The server hashes the values it read from the **raw request body**, so what you hash must be what you send.

Field types (anything else is not signable and is rejected):

| Field | Type |
|---|---|
| `target_exposure_units`, `amount_usd` | decimal string |
| `hedge_ratio_bps` | integer |
| `destination_wallet_address`, `owner_pubkey`, `owner_multisig` | string |

Actions, their endpoints, and exactly which fields are hashed (only those present in the body):

| Action | Endpoint | params |
|---|---|---|
| `edit_hedge_settings` | `PATCH /strategies/{id}` | `hedge_ratio_bps` (integer), `target_exposure_units` (decimal string) |
| `rebalance` | `POST /strategies/{id}/rebalance` | `{}` |
| `return_excess` | `POST /strategies/{id}/withdrawals` | `amount_usd` (decimal string), `destination_wallet_address` |
| `close_strategy` | `DELETE /strategies/{id}` | `destination_wallet_address` |
| `change_owner` | `POST /strategies/{id}/owner` | one of `owner_pubkey` / `owner_multisig` |

Note: for `edit_hedge_settings` the v4 draft sent `target_exposure_units` as a JSON number; it must now be sent (and
signed) as a decimal string.

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
params (canonical)  {"hedge_ratio_bps":7000,"target_exposure_units":"520.5"}
params_hash         470d3a4a3a5e4cc61dc9ebc0f745606045f8ff6095f9bdb2305a0d8f1ddf9104
message             sereel-strategy-v1|solana|FAe4sisG95oZ42w7buUn5qEE4TAnfTTFPiguZUHmhiF|edit_hedge_settings|5b5e4c52-8f1a-4d0e-9a53-2f6f3e1c7a10|470d3a4a3a5e4cc61dc9ebc0f745606045f8ff6095f9bdb2305a0d8f1ddf9104|0b1f6c7e-3a2d-4c1b-9e8f-7d6c5b4a3921|1759577234123
signature (base58)  wDzgfP9LujamT86N5oFrhgQTbTMxCH8MAppFqHFKCBceUssdgCtu4sNmPQu6FpGQ8T18va333LTyc6YDQNwk7Q8
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
| `ORDER_NOT_FILLED` | 400 | no fill after the IOC retries, or the venue rejected the order |
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
