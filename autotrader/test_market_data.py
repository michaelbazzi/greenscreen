"""
Unit tests for market_data.py's indicator math (_rsi, _atr_pct,
_volume_ratio, _sma) with hand-calculated expected values - independent of
propose_trade.py's reverse-engineered test helper, so a bug shared between
the real formula and that helper's inverse wouldn't hide here.
"""

import pytest

import market_data as md


# --- _sma -------------------------------------------------------------

def test_sma_returns_none_with_insufficient_history():
    assert md._sma([100.0, 101.0, 102.0], 5) is None


def test_sma_averages_last_n_closes():
    closes = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert md._sma(closes, 5) == pytest.approx(30.0)
    assert md._sma(closes, 3) == pytest.approx(40.0)  # last 3: 30,40,50


# --- _rsi ---------------------------------------------------------------

def test_rsi_returns_none_with_insufficient_history():
    assert md._rsi([100.0] * 10, period=14) is None


def test_rsi_all_gains_is_100():
    # Strictly increasing by 1 each day -> no losses at all -> RSI = 100
    closes = [100.0 + i for i in range(15)]
    assert md._rsi(closes, period=14) == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    closes = [100.0 - i for i in range(15)]
    assert md._rsi(closes, period=14) == pytest.approx(0.0)


def test_rsi_hand_calculated():
    # 4 up days of +2, then 3 down days of -1 (period=7, needs 8 closes)
    closes = [100, 102, 104, 106, 108, 107, 106, 105]
    # gains: 2,2,2,2 (sum 8) losses: 1,1,1 (sum 3) over period=7
    avg_gain = 8 / 7
    avg_loss = 3 / 7
    expected_rsi = 100 - (100 / (1 + avg_gain / avg_loss))
    assert md._rsi(closes, period=7) == pytest.approx(expected_rsi)


# --- _atr_pct ------------------------------------------------------------

def test_atr_pct_returns_none_with_insufficient_history():
    assert md._atr_pct([101.0] * 10, [99.0] * 10, [100.0] * 10, period=14) is None


def test_atr_pct_constant_range_matches_hand_calculation():
    # Constant daily high-low range of 2, flat closes -> true range is
    # just high-low every day (no gap vs prior close) -> ATR = 2 exactly.
    n = 20
    closes = [100.0] * n
    highs = [101.0] * n
    lows = [99.0] * n
    atr_pct = md._atr_pct(highs, lows, closes, period=14)
    assert atr_pct == pytest.approx(2.0 / 100.0)


# --- _volume_ratio ---------------------------------------------------------

def test_volume_ratio_returns_none_with_insufficient_history():
    assert md._volume_ratio([1000] * 10) is None


def test_volume_ratio_hand_calculated():
    # 15 days of volume=1000 (part of the 20-day window), then last 5
    # days of volume=2000 -> 20d avg = (15*1000 + 5*2000)/20 = 1250,
    # 5d avg = 2000 -> ratio = 2000/1250 = 1.6
    volumes = [1000] * 15 + [2000] * 5
    assert md._volume_ratio(volumes) == pytest.approx(1.6)


# --- technical_score: component behavior -----------------------------

def test_technical_score_all_neutral_inputs_is_half():
    signals = {
        "current_price": 100.0, "sma20": 100.0, "sma50": None, "sma200": None,
        "return_5d": 0.0, "rsi14": None, "atr_pct": None, "volume_ratio": None,
    }
    assert md.technical_score(signals) == pytest.approx(0.5)


def test_technical_score_rewards_low_volatility():
    base = {
        "current_price": 100.0, "sma20": 100.0, "sma50": None, "sma200": None,
        "return_5d": 0.0, "rsi14": None, "volume_ratio": None,
    }
    calm = md.technical_score({**base, "atr_pct": 0.005})
    choppy = md.technical_score({**base, "atr_pct": 0.08})
    assert calm > choppy


def test_technical_score_rewards_higher_rsi():
    base = {
        "current_price": 100.0, "sma20": 100.0, "sma50": None, "sma200": None,
        "return_5d": 0.0, "atr_pct": None, "volume_ratio": None,
    }
    strong = md.technical_score({**base, "rsi14": 80})
    weak = md.technical_score({**base, "rsi14": 20})
    assert strong > weak


def test_technical_score_rewards_longer_term_uptrend():
    base = {
        "current_price": 110.0, "sma20": 110.0, "return_5d": 0.0,
        "rsi14": None, "atr_pct": None, "volume_ratio": None,
    }
    above_long_term_avgs = md.technical_score({**base, "sma50": 100.0, "sma200": 95.0})
    below_long_term_avgs = md.technical_score({**base, "sma50": 120.0, "sma200": 125.0})
    assert above_long_term_avgs > below_long_term_avgs
