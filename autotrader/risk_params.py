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
MIN_CASH_RESERVE_PCT = 0.10      # never let a buy push cash below 10% of portfolio value
MIN_ORDER_NOTIONAL = 10.0        # reject dust orders under $10

# --- Stop-loss --------------------------------------------------------
STOP_LOSS_PCT = -0.15            # core (already-held) positions
STOP_LOSS_PCT_NEW_TICKER = -0.20  # wider stop for newly-admitted, higher-volatility names

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

# --- Deterministic technical-score gate ------------------------------------
# propose_trade.py computes this score itself from live market data via
# market_data.py — it is never accepted as a caller-supplied argument.
TECH_SCORE_THRESHOLD_EXISTING = 0.60     # min score to add to a current holding
TECH_SCORE_THRESHOLD_NEW = 0.75          # min score to admit a brand-new ticker

# --- Circuit breakers ----------------------------------------------------
# Portfolio-level drawdown limits. Tripping either blocks NEW buys only —
# stop-loss sells still fire. Cleared manually (see autotrader/README.md).
DAILY_DRAWDOWN_CIRCUIT_BREAKER_PCT = -0.08
WEEKLY_DRAWDOWN_CIRCUIT_BREAKER_PCT = -0.15

# --- Run coordination -----------------------------------------------------
RUN_LOCK_STALE_MINUTES = 30      # a lockfile older than this is treated as abandoned

# --- Rotation (sell a held position to fund a better-looking candidate) ----
# The primary way this account deploys new capital into new ideas, since it
# never receives fresh deposits - a plain buy will almost always fail the
# cash-reserve check otherwise. Live as of 2026-08-20.
#
# No calendar-based cooldown by design: the technical score is built from
# daily bars (5-day return, price vs. 20-day SMA) and can't meaningfully
# shift within a single trading day across this system's 4 intraday
# cycles, so a same-day reversal is already close to mechanically
# impossible. An arbitrary time limit on top of that would block a
# genuinely correct decision for no reason tied to actual conditions - the
# score-edge requirement below is the real, live-data-grounded protection
# against chasing noise.
ROTATION_ENABLED = True
ROTATION_MIN_SCORE_EDGE = 0.15   # candidate's technical score must beat the
                                  # weakest holding's score by at least this much
