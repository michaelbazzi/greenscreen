"""
Unit tests for propose_trade.py's guardrail logic (evaluate_buy / evaluate_sell) -
the code that decides whether the research agent's proposals are actually
allowed to execute. Alpaca and market-data calls are mocked; only the
decision logic itself is under test.

Run with: /Users/MichaelBazzi/trading-env/bin/python3 -m pytest -v
(from the autotrader/ directory, or anywhere - conftest handles the path)
"""

from types import SimpleNamespace

import pytest

import risk_params as rp
import propose_trade as pt
from db import get_connection, init_decisions_table, log_decision


# --- fixtures ---------------------------------------------------------

@pytest.fixture
def conn():
    """A fresh in-memory decisions DB per test."""
    c = get_connection(":memory:")
    init_decisions_table(c)
    yield c
    c.close()


def make_position(market_value, avg_entry_price=100.0, current_price=100.0, qty=1.0):
    return SimpleNamespace(
        market_value=market_value,
        avg_entry_price=avg_entry_price,
        current_price=current_price,
        qty=qty,
    )


def make_snapshot(cash, portfolio_value, positions=None):
    return {
        "account_id": "test-account",
        "cash": cash,
        "portfolio_value": portfolio_value,
        "positions": positions or {},
    }


def make_signals(current_price=110.0, sma20=100.0, return_5d=0.03,
                  avg_dollar_volume=10_000_000, trading_days_available=250):
    """current_price > sma20 and positive return_5d => a healthy technical
    score comfortably above both thresholds, unless a test overrides it."""
    return {
        "ticker": "TEST",
        "current_price": current_price,
        "sma20": sma20,
        "return_5d": return_5d,
        "avg_dollar_volume": avg_dollar_volume,
        "trading_days_available": trading_days_available,
        "first_bar_date": None,
    }


# --- evaluate_buy: basic gates ---------------------------------------

def test_buy_rejects_dust_order(conn):
    snapshot = make_snapshot(cash=500, portfolio_value=1000)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 1.0, None, conn)
    assert score is None
    assert "below floor" in reason


def test_buy_rejects_when_no_price_history(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: None)
    snapshot = make_snapshot(cash=500, portfolio_value=1000)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert score is None
    assert "insufficient price history" in reason


def test_buy_approved_when_all_checks_pass(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # AAPL already held, plenty of cash and position headroom
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert reason is None
    assert score >= rp.TECH_SCORE_THRESHOLD_EXISTING


# --- evaluate_buy: technical score gate --------------------------------

def test_buy_rejects_below_technical_score_threshold(conn, monkeypatch):
    # current_price == sma20 and flat return_5d => score lands at 0.5,
    # below TECH_SCORE_THRESHOLD_EXISTING (0.60 by default)
    flat_signals = make_signals(current_price=100.0, sma20=100.0, return_5d=0.0)
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: flat_signals)
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert score == pytest.approx(0.5)
    assert "technical score" in reason


# --- evaluate_buy: sizing / position caps ------------------------------

def test_buy_rejects_trade_exceeding_max_trade_pct(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    oversized = 1000 * rp.MAX_TRADE_PCT + 1
    score, reason = pt.evaluate_buy(snapshot, "AAPL", oversized, None, conn)
    assert "max trade size" in reason


def test_buy_rejects_exceeding_max_position_pct(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # Already holding right up against the position cap
    already_held = 1000 * rp.MAX_POSITION_PCT - 5
    positions = {"AAPL": make_position(market_value=already_held)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 10.0, None, conn)
    assert "max position size" in reason


def test_buy_rejects_insufficient_cash_reserve(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {"AAPL": make_position(market_value=100.0)}
    # cash is basically zero, like the real live account
    snapshot = make_snapshot(cash=0.04, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert "reserve floor" in reason


# --- evaluate_buy: new-ticker admission ---------------------------------

def test_buy_rejects_new_ticker_with_insufficient_history(conn, monkeypatch):
    thin_signals = make_signals(trading_days_available=3)
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: thin_signals)
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions={})
    score, reason = pt.evaluate_buy(snapshot, "FRESHIPO", 40.0, None, conn)
    assert "trading days of history" in reason


def test_buy_rejects_new_ticker_when_at_max_tickers_held(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    positions = {
        f"T{i}": make_position(market_value=10.0)
        for i in range(rp.MAX_TICKERS_HELD)
    }
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "NEWTICKER", 40.0, "technology", conn)
    assert "at max" in reason


def test_buy_uses_wider_criteria_and_smaller_cap_for_new_tickers(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions={})
    # new-ticker cap is smaller than the existing-holding cap
    just_over_new_cap = 1000 * rp.MAX_TRADE_PCT_NEW_TICKER + 1
    score, reason = pt.evaluate_buy(snapshot, "NEWTICKER", just_over_new_cap, "technology", conn)
    assert "max trade size" in reason


# --- evaluate_buy: daily caps -------------------------------------------

def test_buy_rejects_when_daily_buy_count_reached(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    today = pt.today_str()
    for i in range(rp.MAX_NEW_POSITIONS_PER_DAY):
        log_decision(conn, (
            f"{today}T00:00:0{i}Z", f"run-{i}", "SOMETICKER", "buy",
            "notional", 10.0, 0.9, "test fill", None, 1, None, f"order-{i}",
        ))
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert "buys today" in reason


def test_buy_rejects_when_daily_notional_cap_reached(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    today = pt.today_str()
    daily_cap = 1000 * rp.MAX_DAILY_NOTIONAL_DEPLOYED_PCT
    log_decision(conn, (
        f"{today}T00:00:00Z", "run-x", "SOMETICKER", "buy",
        "notional", daily_cap - 5, 0.9, "test fill", None, 1, None, "order-x",
    ))
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "AAPL", 40.0, None, conn)
    assert "deploy $" in reason


# --- evaluate_buy: sector exposure --------------------------------------

def test_buy_rejects_exceeding_sector_exposure_cap(conn, monkeypatch):
    monkeypatch.setattr(pt.md, "compute_signals", lambda ticker: make_signals())
    # AAPL and MSFT are both tagged "technology" in risk_params
    already_held = 1000 * rp.MAX_SECTOR_EXPOSURE_PCT - 5
    positions = {"AAPL": make_position(market_value=already_held)}
    snapshot = make_snapshot(cash=5000, portfolio_value=1000, positions=positions)
    score, reason = pt.evaluate_buy(snapshot, "MSFT", 40.0, None, conn)
    assert "sector" in reason


# --- evaluate_sell -------------------------------------------------------

def test_sell_rejects_dust_order():
    snapshot = make_snapshot(cash=0, portfolio_value=1000,
                              positions={"AAPL": make_position(market_value=100.0)})
    reason = pt.evaluate_sell(snapshot, "AAPL", 1.0)
    assert "below floor" in reason


def test_sell_rejects_ticker_not_held():
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions={})
    reason = pt.evaluate_sell(snapshot, "AAPL", 40.0)
    assert "no open position" in reason


def test_sell_rejects_more_than_held_value():
    positions = {"AAPL": make_position(market_value=50.0)}
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions=positions)
    reason = pt.evaluate_sell(snapshot, "AAPL", 100.0)
    assert "exceeds held value" in reason


def test_sell_approved_within_held_value():
    positions = {"AAPL": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=0, portfolio_value=1000, positions=positions)
    reason = pt.evaluate_sell(snapshot, "AAPL", 40.0)
    assert reason is None
