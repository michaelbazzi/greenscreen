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
from db import get_connection, init_decisions_table, log_decision, rotation_already_logged


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


# --- evaluate_rotation ----------------------------------------------------

def _signals_with_score(target_score, sma20=100.0):
    """Reverse-engineer signals that produce exactly `target_score` from
    technical_score()'s formula (0.5 * trend_norm + 0.5 * momentum_norm,
    each independently mapped from a +/-10% range to [0,1]). Drives trend
    and momentum to the same normalized value so their average equals the
    target exactly, rather than skewing one dimension and risking clamping
    at the +/-10% edges."""
    raw = target_score * 0.20 - 0.10  # inverse of the (+0.10)/0.20 mapping
    current_price = sma20 * (1 + raw)
    return make_signals(current_price=current_price, sma20=sma20, return_5d=raw)


def test_rotation_rejected_when_disabled(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", False)
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=100, portfolio_value=1000, positions=positions)
    from_score, to_score, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "ROTATION_ENABLED" in reason


def test_rotation_rejected_when_from_ticker_not_held(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    snapshot = make_snapshot(cash=100, portfolio_value=1000, positions={})
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "not currently held" in reason


def test_rotation_rejected_when_from_is_not_weakest_holding(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    # JPM scores well (0.8), AAPL scores poorly (0.3) - AAPL is the real
    # weak link, so rotating JPM should be refused even if MRNA looks great.
    scores = {"JPM": 0.80, "AAPL": 0.30, "MRNA": 0.95}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {
        "JPM": make_position(market_value=100.0),
        "AAPL": make_position(market_value=100.0),
    }
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "not the weakest-scoring holding" in reason
    assert "AAPL" in reason


def test_rotation_rejected_when_edge_too_small(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    # JPM is the only (and thus weakest) holding; MRNA barely beats it -
    # edge below ROTATION_MIN_SCORE_EDGE (0.15 default).
    scores = {"JPM": 0.70, "MRNA": 0.75}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert "edge" in reason


def test_rotation_allowed_even_when_from_ticker_bought_moments_ago(conn, monkeypatch):
    """No calendar cooldown by design - see the comment in evaluate_rotation.
    A position bought seconds ago is still eligible for rotation if it's
    genuinely the weakest holding and the candidate clears the score edge."""
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    from db import log_trade
    log_trade(conn, (
        pt.now_iso(), "acct", "JPM", "buy", "market", None, 100.0,
        None, None, "filled", "order-1", 1,
    ))
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert reason is None


def test_rotation_approved_when_all_checks_pass(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    from_score, to_score, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert reason is None
    assert from_score == pytest.approx(0.50, abs=0.01)
    assert to_score == pytest.approx(0.90, abs=0.01)


def test_rotation_ignores_cash_reserve_but_plain_buy_still_respects_it(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    scores = {"JPM": 0.50, "MRNA": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    positions = {"JPM": make_position(market_value=100.0)}
    # cash is already deep below the 10% reserve floor and a same-size
    # swap can never raise it - rotation should still be approved.
    snapshot = make_snapshot(cash=0.04, portfolio_value=1000, positions=positions)
    from_score, to_score, reason = pt.evaluate_rotation(snapshot, "JPM", "MRNA", 40.0, None, conn)
    assert reason is None

    # But a plain (non-rotation) buy against the same starved-cash snapshot
    # must still be rejected on the reserve floor - this exemption is
    # rotation-specific, not a global loosening of the cash check.
    score, buy_rejection = pt.evaluate_buy(snapshot, "MRNA", 40.0, None, conn)
    assert "reserve floor" in buy_rejection


def test_rotation_already_logged_scoped_to_exact_pair(conn):
    log_decision(conn, (
        pt.now_iso(), "run-1", "MRNA", "buy", "notional", 40.0,
        0.9, "[ROTATION: JNJ -> MRNA] original attempt", None, 0,
        "JNJ is not the weakest holding", None,
    ), counterparty="JNJ")
    # Exact same pair, same run - blocked.
    assert rotation_already_logged(conn, "run-1", "JNJ", "MRNA") is True
    # Corrected retry with a different from_ticker for the same candidate -
    # NOT blocked, since this is the legitimate "the gate told me the real
    # weakest holding, retrying with the correction" case.
    assert rotation_already_logged(conn, "run-1", "JPM", "MRNA") is False
    # Different run entirely - not blocked either.
    assert rotation_already_logged(conn, "run-2", "JNJ", "MRNA") is False


def test_decision_already_logged_ignores_rotation_rows(conn):
    """A plain propose buy/sell check must not be confused by rotation
    legs for the same ticker+action - they're logged with a real
    counterparty, plain proposals always use ''."""
    log_decision(conn, (
        pt.now_iso(), "run-1", "MRNA", "buy", "notional", 40.0,
        0.9, "[ROTATION: JPM -> MRNA]", None, 1, None, "order-1",
    ), counterparty="JPM")
    from db import decision_already_logged
    assert decision_already_logged(conn, "run-1", "MRNA", "buy") is False


def test_rotation_rejected_when_buy_leg_fails_sector_cap(conn, monkeypatch):
    monkeypatch.setattr(rp, "ROTATION_ENABLED", True)
    # AAPL scores higher than JPM so JPM stays the weakest holding (the
    # thing actually under test here is the sector-cap rejection, not the
    # weakest-holding check).
    scores = {"JPM": 0.50, "AAPL": 0.60, "MSFT": 0.90}
    monkeypatch.setattr(pt.md, "compute_signals",
                         lambda ticker: _signals_with_score(scores[ticker]))
    # AAPL (technology) already sits right at the sector cap; rotating into
    # MSFT (also technology) should blow through it even though MSFT wins
    # on score and JPM is the weakest holding.
    already_held_tech = 1000 * rp.MAX_SECTOR_EXPOSURE_PCT - 5
    positions = {
        "JPM": make_position(market_value=100.0),
        "AAPL": make_position(market_value=already_held_tech),
    }
    snapshot = make_snapshot(cash=500, portfolio_value=1000, positions=positions)
    _, _, reason = pt.evaluate_rotation(snapshot, "JPM", "MSFT", 40.0, None, conn)
    assert "buy leg would fail" in reason
    assert "sector" in reason
