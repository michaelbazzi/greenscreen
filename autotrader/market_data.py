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

The score blends seven components, all plain arithmetic on real price/
volume bars -- deliberately nothing that requires viewing a chart or
subjective pattern-reading (head-and-shoulders, breakouts, etc.), since
that kind of call isn't independently verifiable the way a number is.
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

# ~200 trading days for the 200-day SMA needs roughly 290 calendar days;
# padded further for weekends/holidays and to give shorter-history tickers
# more room before their long-term components fall back to neutral.
LOOKBACK_DAYS = 320


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _normalize(x, lo, hi):
    """Clip x to [lo, hi] and rescale linearly to [0, 1]."""
    return (_clip(x, lo, hi) - lo) / (hi - lo)


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


def _sma(closes, n):
    """None if there isn't enough history for a proper N-day average -
    callers treat a None component as neutral rather than penalizing a
    ticker just for having a shorter trading history."""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _rsi(closes, period=14):
    """Standard 0-100 relative strength index over the trailing `period`
    daily changes (simple, not Wilder-smoothed - adequate for a screening
    signal, not meant for high-frequency precision)."""
    if len(closes) < period + 1:
        return None
    window = closes[-(period + 1):]
    changes = [window[i] - window[i - 1] for i in range(1, len(window))]
    gains = [c for c in changes if c > 0]
    losses = [-c for c in changes if c < 0]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _atr_pct(highs, lows, closes, period=14):
    """Average True Range as a fraction of current price - a volatility
    measure independent of the stock's absolute price level."""
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(len(closes) - period, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    atr = sum(true_ranges) / period
    return atr / closes[-1]


def _volume_ratio(volumes):
    """Recent (5-day) average volume relative to the 20-day average -
    elevated volume behind a move suggests more conviction than the same
    move on thin volume."""
    if len(volumes) < 20:
        return None
    avg_5 = sum(volumes[-5:]) / 5
    avg_20 = sum(volumes[-20:]) / 20
    if avg_20 == 0:
        return None
    return avg_5 / avg_20


def compute_signals(ticker):
    """Returns None if there isn't enough bar history to compute signals
    (e.g. a ticker that just started trading)."""
    bars = get_bars(ticker)
    if len(bars) < 2:
        return None

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    volumes = [b.volume for b in bars]

    current_price = closes[-1]
    sma20 = _sma(closes, 20) or current_price  # always available past the
    # 20-trading-day admission floor; the "or" is just defensive.

    lookback_5 = min(5, len(closes) - 1)
    return_5d = (closes[-1] / closes[-1 - lookback_5]) - 1 if lookback_5 > 0 else 0.0

    avg_dollar_volume = sum(c * v for c, v in zip(closes[-20:], volumes[-20:])) / len(closes[-20:])

    return {
        "ticker": ticker,
        "current_price": current_price,
        "sma20": sma20,
        "sma50": _sma(closes, 50),
        "sma200": _sma(closes, 200),
        "return_5d": return_5d,
        "avg_dollar_volume": avg_dollar_volume,
        "rsi14": _rsi(closes, 14),
        "atr_pct": _atr_pct(highs, lows, closes, 14),
        "volume_ratio": _volume_ratio(volumes),
        "trading_days_available": len(bars),
        "first_bar_date": bars[0].timestamp,
    }


# Component weights - must sum to 1.0. Short-term trend/momentum still
# carry the most weight (the original two-input score, validated across
# tonight's live runs); the newer components add context without
# dominating: is this move confirmed by broader trend structure, healthy
# momentum (not overextended), real volume, and reasonable volatility.
_WEIGHTS = {
    "trend20": 0.20,
    "momentum5": 0.15,
    "rsi14": 0.15,
    "trend50": 0.15,
    "trend200": 0.15,
    "volatility": 0.10,
    "volume": 0.10,
}


def technical_score(signals):
    """Deterministic 0-1 score. Every component is plain arithmetic on
    real price/volume bars, normalized and clipped so no single wild
    outlier can saturate the score. A component that can't be computed
    (not enough history for that specific window) defaults to a neutral
    0.5 rather than penalizing shorter-history tickers or crashing."""
    price = signals["current_price"]

    trend20 = _normalize((price / signals["sma20"]) - 1, -0.10, 0.10)
    momentum5 = _normalize(signals["return_5d"], -0.10, 0.10)

    rsi14 = signals.get("rsi14")
    rsi_norm = _normalize(rsi14, 0, 100) if rsi14 is not None else 0.5

    sma50 = signals.get("sma50")
    trend50 = _normalize((price / sma50) - 1, -0.15, 0.15) if sma50 else 0.5

    sma200 = signals.get("sma200")
    trend200 = _normalize((price / sma200) - 1, -0.20, 0.20) if sma200 else 0.5

    atr_pct = signals.get("atr_pct")
    # Inverted: lower volatility scores higher - "aggressive growth, not
    # reckless" favors a controlled move over a wildly choppy one at the
    # same trend/momentum reading.
    volatility = (1 - _normalize(atr_pct, 0.005, 0.08)) if atr_pct is not None else 0.5

    volume_ratio = signals.get("volume_ratio")
    # Centered on 1.0x (average volume) = neutral 0.5, up to 1.5x+ = 1.0.
    volume = _normalize(volume_ratio, 0.5, 1.5) if volume_ratio is not None else 0.5

    score = (
        _WEIGHTS["trend20"] * trend20
        + _WEIGHTS["momentum5"] * momentum5
        + _WEIGHTS["rsi14"] * rsi_norm
        + _WEIGHTS["trend50"] * trend50
        + _WEIGHTS["trend200"] * trend200
        + _WEIGHTS["volatility"] * volatility
        + _WEIGHTS["volume"] * volume
    )
    return round(score, 4)


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
