"""
Technical signals pulled from Alpaca's own market data (same paper keys,
no separate API/key needed).

Two jobs:
  1. Compute a deterministic technical score (0-1) that propose_trade.py
     gates trades on. This is intentionally NOT something the research step
     can set directly -- it's recomputed here from live data every time, so
     a persuasive rationale alone can never satisfy the numeric gate.
  2. Validate new-ticker admission criteria (trading history length as an
     IPO-age proxy, average dollar volume as a liquidity floor). Alpaca has
     no "recently IPO'd" endpoint -- discovery of candidates has to come
     from the research step's web search; this module only validates.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config import API_KEY, API_SECRET

_data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

LOOKBACK_DAYS = 60  # calendar days of daily bars to pull; ~40 trading days


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def get_bars(ticker, lookback_days=LOOKBACK_DAYS):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,  # free/paper subscriptions can't query recent SIP data
    )
    bars = _data_client.get_stock_bars(request)
    frame = bars.data.get(ticker, [])
    return sorted(frame, key=lambda b: b.timestamp)


def compute_signals(ticker):
    """Returns None if there isn't enough bar history to compute signals
    (e.g. a ticker that just started trading)."""
    bars = get_bars(ticker)
    if len(bars) < 2:
        return None

    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]

    current_price = closes[-1]
    sma20 = sum(closes[-20:]) / len(closes[-20:])

    lookback_5 = min(5, len(closes) - 1)
    return_5d = (closes[-1] / closes[-1 - lookback_5]) - 1 if lookback_5 > 0 else 0.0

    avg_dollar_volume = sum(c * v for c, v in zip(closes[-20:], volumes[-20:])) / len(closes[-20:])

    return {
        "ticker": ticker,
        "current_price": current_price,
        "sma20": sma20,
        "return_5d": return_5d,
        "avg_dollar_volume": avg_dollar_volume,
        "trading_days_available": len(bars),
        "first_bar_date": bars[0].timestamp,
    }


def technical_score(signals):
    """Deterministic 0-1 score blending trend (price vs SMA20) and
    short-term momentum (5-day return). Both components are clipped to a
    +/-10% range and rescaled to [0,1] before averaging, so a single wild
    outlier can't saturate the score."""
    trend = (signals["current_price"] / signals["sma20"]) - 1
    trend_norm = (_clip(trend, -0.10, 0.10) + 0.10) / 0.20

    momentum_norm = (_clip(signals["return_5d"], -0.10, 0.10) + 0.10) / 0.20

    return round(0.5 * trend_norm + 0.5 * momentum_norm, 4)


def meets_new_ticker_criteria(signals, min_trading_days, min_avg_dollar_volume):
    """Returns (ok: bool, reason: str | None)."""
    if signals["trading_days_available"] < min_trading_days:
        return False, (
            f"only {signals['trading_days_available']} trading days of history "
            f"available, need >= {min_trading_days}"
        )
    if signals["avg_dollar_volume"] < min_avg_dollar_volume:
        return False, (
            f"avg dollar volume ${signals['avg_dollar_volume']:,.0f}/day "
            f"below floor ${min_avg_dollar_volume:,.0f}/day"
        )
    return True, None
