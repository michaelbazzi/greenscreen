"""
Every hard-coded guardrail the autonomous trader is allowed to act within.

This is the file to edit when tuning behavior. Nothing execution-relevant
should live as a magic number anywhere else in autotrader/.
"""

# --- Master kill switch --------------------------------------------------
# Flip to False to instantly stop all autonomous execution (sweeps and
# proposals both). Checked first, before anything else, in propose_trade.py.
ENABLED = True

# --- Position & trade sizing ----------------------------------------------
MAX_POSITION_PCT = 0.25          # a single ticker can't exceed 25% of portfolio value
MAX_TRADE_PCT = 0.10             # a single order can't exceed 10% of portfolio value
MAX_TRADE_PCT_NEW_TICKER = 0.05  # half-size for tickers not already held
# Lowered 10% -> 5% on 2026-09-12. In a long-only account with no margin and
# no fresh deposits, this floor isn't protecting against a margin call - it's
# reserving the ability to act on a new idea without first selling something.
# 10% was costing more than it protected: on a ~$1,128 book it held back $113
# while the largest allowed new position was $56, so roughly two positions'
# worth of capital sat permanently idle in a rising market. 5% keeps about one
# position in reserve, which is what the floor is actually for.
MIN_CASH_RESERVE_PCT = 0.05      # never let a buy push cash below 5% of portfolio value
MIN_ORDER_NOTIONAL = 10.0        # reject dust orders under $10

# --- Stop-loss --------------------------------------------------------
STOP_LOSS_PCT = -0.15            # core (already-held) positions
STOP_LOSS_PCT_NEW_TICKER = -0.20  # wider stop for newly-admitted, higher-volatility names

# --- Manually-held positions (hands-off for the automated system) --------
# Tickers in this set are excluded from ALL automated sell paths - the
# stop-loss sweep, an explicit sell proposal, and rotation (as the FROM
# leg) - regardless of price movement or technical score. For a position
# the account holder bought and manages themselves, not something the
# autonomous system is meant to touch at all. Add/remove tickers here as
# needed; empty by default (nothing is excluded unless explicitly listed).
MANUALLY_HELD_TICKERS = {"BTCUSD"}  # bought manually 2026-09-10, hands-off by request

# --- Daily/volume caps -----------------------------------------------------
MAX_NEW_POSITIONS_PER_DAY = 4    # new BUYs only; stop-loss sells are exempt/uncapped
MAX_DAILY_NOTIONAL_DEPLOYED_PCT = 0.30  # total new spend per day, as % of portfolio value

# --- Portfolio shape --------------------------------------------------
MAX_TICKERS_HELD = 10
MAX_SECTOR_EXPOSURE_PCT = 0.40
MAX_SECTOR_EXPOSURE_PCT_UNKNOWN = 0.15   # tighter cap if a ticker's sector isn't tagged

# Known sector tags for the current core holdings. New tickers must be
# passed a --sector by the caller; unrecognized/omitted sectors fall back
# to "unknown" and the tighter exposure cap above.
TICKER_SECTORS = {
    "AAPL": "technology",
    "MSFT": "technology",
    "JPM": "financials",
    "XOM": "energy",
    "JNJ": "healthcare",
}

# --- New-ticker (IPO/candidate) admission criteria -------------------------
NEW_TICKER_MIN_TRADING_DAYS = 20         # avoid first-days-of-listing whipsaw
NEW_TICKER_MIN_AVG_DOLLAR_VOLUME = 5_000_000  # daily $ volume liquidity floor

# --- Deterministic technical-score gate (DEMOTED to informational-only,
# 2026-09-11) ----------------------------------------------------------------
# propose_trade.py still computes this score itself from live market data
# via market_data.py on every proposal — it is never accepted as a
# caller-supplied argument, and it's still logged on every decision — but
# evaluate_buy no longer REJECTS a proposal for scoring low. GreenScreen-
# backtest's evidence (a decade of data, in-sample and held-out, composite
# score and every individual component) found the score has no positive,
# and a mildly negative, correlation with actual forward returns for this
# universe. Gating trade admission on a number shown to point the wrong
# way was doing real harm, not just adding noise. Selection is now the
# research agent's own judgment call — real-time WebSearch, filings,
# catalysts — gated only by the risk limits below (cash reserve, position/
# trade size caps, sector exposure, count/daily caps, stop-loss, circuit
# breakers), none of which this backtest evidence questioned.
#
# These two constants are kept, unused by evaluate_buy, as a record of
# what the gate used to require and a fast path back if this doesn't pan
# out - restoring the score check is re-adding the two-line comparison in
# evaluate_buy, not rederiving these numbers.
TECH_SCORE_THRESHOLD_EXISTING = 0.60     # historical value - NOT enforced, see above
TECH_SCORE_THRESHOLD_NEW = 0.75          # historical value - NOT enforced, see above

# --- Circuit breakers ----------------------------------------------------
# Portfolio-level drawdown limits. Tripping either blocks NEW buys only —
# stop-loss sells still fire. Cleared manually (see autotrader/README.md).
DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT = -0.08
WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT = -0.15

# --- Circuit-breaker ramp-down (added 2026-09-12) --------------------------
# A cleared breaker used to resume new-buy sizing at FULL MAX_TRADE_PCT /
# MAX_POSITION_PCT the very next cycle - the daily one auto-clearing
# overnight, the weekly one via reset-circuit-breaker, either way straight
# back to full size as though nothing had happened. A drawdown deep enough
# to trip a breaker is itself evidence the next few cycles deserve smaller
# size, not an instant return to normal.
#
# So: for CIRCUIT_BREAKER_RAMP_HOURS after the most recent trip of EITHER
# kind, new-buy size caps are scaled by CIRCUIT_BREAKER_RAMP_MULTIPLIER.
# The window is measured from the last TRIP, not from the clear, and it
# deliberately survives reset-circuit-breaker - otherwise "clear it" would
# still be a one-command return to full size, which is the exact behavior
# this exists to remove.
#
# Wall-clock hours rather than a countdown of N cycles: a cycle counter has
# to be decremented by something, which means either a mutable counter that
# concurrent cycles can race on, or a scan of the decisions table. A
# timestamp comparison is idempotent and stateless to read. At this
# system's ~4 cycles per trading day, 48h is roughly 8 cycles, and it errs
# long across a weekend rather than short.
#
# Applies to new BUYS only - same scope as the breakers themselves. The
# stop-loss sweep and evaluate_sell are untouched: getting OUT must never
# be throttled by a risk control.
CIRCUIT_BREAKER_RAMP_HOURS = 48
CIRCUIT_BREAKER_RAMP_MULTIPLIER = 0.5

# --- Run coordination -----------------------------------------------------
RUN_LOCK_STALE_MINUTES = 30      # a lockfile older than this is treated as abandoned

# --- Rotation (sell a held position to fund a better-looking candidate) ----
# DISABLED 2026-09-11 - rotation's entire trigger is a comparison of two
# technical scores (sell whichever holding scores weakest, buy whichever
# candidate beats it by ROTATION_MIN_SCORE_EDGE), and GreenScreen-backtest's
# evidence found that score has no positive - and a mildly negative -
# correlation with forward returns. A mechanism built entirely on comparing
# a signal shown to point the wrong way has no principled basis left, same
# reasoning as demoting evaluate_buy's score gate above. Funding a new idea
# in this permanently cash-starved account (it never receives fresh
# deposits) now goes through two independent, ungated research-agent
# judgment calls instead of one scored swap: an explicit evaluate_sell of
# whatever holding the agent itself decides no longer merits the position,
# then a separate evaluate_buy for the new candidate - both gated only by
# the risk limits, neither by a score comparison. Was live 2026-08-20 to
# 2026-09-11; ROTATION_MIN_SCORE_EDGE is kept, unused, as a record of what
# the requirement used to be.
ROTATION_ENABLED = False
ROTATION_MIN_SCORE_EDGE = 0.15   # historical value - NOT enforced while rotation is disabled
