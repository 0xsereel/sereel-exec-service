"""API errors. Every non-2xx body is exactly {"error": <human message>, "code": <CODE>} (Cantina v4 contract).

CODES is the registry of every code the service emits. Cantina handles some of them specially, so their spelling is
fixed: AUTHORIZATION_REQUIRED, STALE_PRICE, PRICE_DEVIATION, INSUFFICIENT_MARGIN. A test checks that every code used in
the source is registered here and documented in the README.
"""

# code -> (usual HTTP status, meaning)
CODES: dict[str, tuple[int, str]] = {
    # --- codes Cantina handles specifically (spelling is part of the contract) ---
    "AUTHORIZATION_REQUIRED": (401, "a signed-message `authorization` is missing on a call that needs one"),
    "STALE_PRICE": (503, "the Pyth price is older than the market's max_staleness_s while the market is open, or Pyth is unreachable"),
    "PRICE_DEVIATION": (400, "the venue mark differs from the Pyth price by more than MAX_PRICE_DEVIATION_BPS; no order was sent"),
    "INSUFFICIENT_MARGIN": (400, "not enough margin on the venue (or in the account) for the requested position"),
    # --- other signed-authorization failures ---
    "AUTHORIZATION_INVALID": (403, "the signed-message authorization failed verification (bad signature, wrong params, expired or replayed)"),
    # --- generic ---
    "CHAIN_UNAVAILABLE": (503, "a Solana read needed for the decision (e.g. a Squads multisig's members) failed"),
    "UNAUTHORIZED": (401, "missing or invalid X-Sereel-Key"),
    "BAD_REQUEST": (400, "malformed or invalid input"),
    "NOT_FOUND": (404, "unknown id or route"),
    "CONFLICT": (409, "the request conflicts with the current state (e.g. cancelling an already active strategy)"),
    "HTTP_ERROR": (0, "any other HTTP error (the status is the HTTP status of the error, e.g. 405)"),
    "INTERNAL": (500, "unexpected error (details are logged, never returned)"),
    "NOT_CONFIGURED": (503, "a required setting is missing (e.g. STABLECOIN_MINT)"),
    # --- trading / venue ---
    "UNKNOWN_MARKET": (404, "the market id is not in markets.yaml (or not found on the venue)"),
    "NO_LIQUIDITY": (409, "the order book does not offer enough size within slippage of the mark to close the position; nothing was sent (start the market maker and retry)"),
    "ORDER_NOT_FILLED": (400, "no fill after the IOC retries, or the venue rejected the order"),
    "VENUE_UNAVAILABLE": (503, "the venue could not be reached or returned no price, so a live value cannot be computed"),
    "LEVERAGE_NOT_SET": (503, "the venue did not confirm the configured leverage for the market, so no order was sent"),
    "VENUE_NOT_CONFIGURED": (503, "Hyperliquid credentials are not set"),
    "PRICE_SOURCE_AUTH": (502, "Pyth Hermes rejected the credentials (PYTH_API_KEY)"),
    # --- withdrawals ---
    "WITHDRAW_BELOW_MARGIN": (400, "the withdrawal would leave the strategy below its required margin, or exceeds the withdrawable balance"),
    "WITHDRAW_NOT_AUTHORIZED": (503, "withdrawing needs the master key, which is not configured"),
    "WITHDRAW_FAILED": (400, "the bridge rejected the withdrawal"),
    # --- payments ---
    "PAYMENT_FAILED": (502, "the Solana payout failed"),
    # --- AI agent ---
    "SIGNALS_UNAVAILABLE": (503, "the agent could not read enough market data (neither Pyth nor Hyperliquid) to take a decision this cycle"),
    "DELEGATE_LIMIT_EXCEEDED": (403, "a delegate's rebalance would exceed the limits the owner signed (daily size, or a forced rebalance on a band-only grant)"),
    "DELEGATE_NOT_ALLOWED": (403, "the signer is a delegate but this action is not allowed for delegates (only rebalance is), or its grant has expired or been revoked"),
    "DATA_FEED_DISABLED": (404, "no data feed is available for that id (disabled or unknown: the answer is identical, so nothing about a strategy leaks)"),
    "PAYMENT_INVALID": (402, "the x402 payment was not accepted (missing, malformed, underpaid, replayed or rejected by the facilitator)"),
    "FACILITATOR_UNAVAILABLE": (503, "the x402 facilitator could not be reached, so a payment could be neither verified nor settled; nothing was charged by the service"),
    "RATE_LIMITED": (429, "too many requests to the public data-feed route from this payer or address"),
    "AGENT_DISABLED": (503, "the AI agent is switched off (AGENT_ENABLED=false)"),
    "LLM_UNAVAILABLE": (503, "the language model could not be reached or is not configured; the chat cannot respond (use the manual form)"),
    "CHAT_SESSION_EXPIRED": (410, "the chat session is older than 24 hours; start a new one"),
    "CHAT_LIMIT_REACHED": (429, "too many chat messages: the session reached its message cap or the owner is sending too fast"),
    # --- market maker (CLI only; never returned by the API) ---
    "MM_MAINNET_REFUSED": (0, "the market maker refuses to run unless HL_API_URL is testnet"),
    "MM_SIZE_OUT_OF_RANGE": (0, "market maker order size outside MM_MIN_SIZE..MM_MAX_SIZE"),
    "MM_BAD_CONFIG": (0, "invalid market maker settings"),
}


class ServiceError(Exception):
    status = 400

    headers: dict | None = None  # extra response headers (e.g. PAYMENT-REQUIRED on a 402)

    def __init__(self, code: str, message: str, status: int | None = None, headers: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.headers = code, message, headers
        if status:
            self.status = status
